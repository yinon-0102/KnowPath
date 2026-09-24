"""Hybrid retrieval over caller-pinned, SQL-authorized whole source chunks.

The caller resolves manifests and verifies immutable source spans before calling
this module. Plugin output carries identities only; it is never evidence text.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import time
from threading import RLock

from .bm25 import BM25Index, TOKENIZER_VERSION, validate_limit
from .contracts import Candidate, SourceSpan
from .fusion import reciprocal_rank_fusion
from .retrieval import RetrievalError, content_hash


def check_deadline(request):
    if time.monotonic() >= request.deadline:
        raise RetrievalError("RETRIEVAL_DEADLINE_EXCEEDED")


def validate_chunks(chunks):
    """Reject ambiguous identities and unmapped evidence before any model call."""
    rows = list(chunks)
    seen = set()
    try:
        for row in rows:
            candidate = Candidate(chunk_id=row["chunk_id"], material_version_id=row["material_version_id"],
                retrieval_version_id=row["retrieval_version_id"], parent_id=row.get("parent_id"), channel="hybrid", rank=1)
            if candidate.chunk_id in seen or not row["source_spans"]:
                raise ValueError()
            seen.add(candidate.chunk_id)
            spans = [span if isinstance(span, SourceSpan) else SourceSpan.model_validate(span) for span in row["source_spans"]]
            if any(span.material_version_id != candidate.material_version_id for span in spans):
                raise ValueError()
            if any(not isinstance(row[key], str) or not row[key].strip() for key in ("source_text", "retrieval_text")):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RetrievalError("RETRIEVAL_SCOPE_INVALID") from None
    return rows


class OrdinaryPlugin:
    name = "ordinary"

    def __init__(self, embedder, dense, *, bm25_profile=None, cache_size=8, shared_cache=None, cache_lock=None):
        if type(cache_size) is not int or not 1 <= cache_size <= 128:
            raise ValueError("BM25 cache size must be between 1 and 128")
        self.embedder, self.dense = embedder, dense
        if bm25_profile is not None and not isinstance(bm25_profile, dict):
            raise ValueError("BM25 profile must be an object")
        # The profile forms part of each cache identity. Questions never affect
        # index construction; BM25Index validates parameters before embedding.
        self.bm25_profile = {"tokenizer": TOKENIZER_VERSION, "k1": 1.5, "b": .75}
        self.bm25_profile.update(bm25_profile or {})
        self.cache_size = cache_size
        self._bm25_cache = shared_cache if shared_cache is not None else OrderedDict()
        self._cache_lock = cache_lock or RLock()

    def _index(self, chunks):
        with self._cache_lock:
            return self._cached_index(chunks)

    def _cached_index(self, chunks):
        identity = sorted((r["retrieval_version_id"], r["material_version_id"], r["chunk_id"], content_hash(r["retrieval_text"])) for r in chunks)
        key = content_hash(json.dumps([self.bm25_profile, identity], sort_keys=True, ensure_ascii=False, allow_nan=False))
        if key in self._bm25_cache:
            self._bm25_cache.move_to_end(key)
            return self._bm25_cache[key], True
        index = BM25Index(chunks, self.bm25_profile)
        self._bm25_cache[key] = index
        if len(self._bm25_cache) > self.cache_size:
            self._bm25_cache.popitem(last=False)
        return index, False

    @staticmethod
    def _check_ranking(ranking, allowed, limit):
        try:
            rows = list(ranking)
            seen = set()
            if len(rows) > limit:
                raise ValueError()
            for identifier, score in rows:
                if (identifier not in allowed or identifier in seen or type(score) not in (int, float)
                        or not math.isfinite(score)):
                    raise ValueError()
                seen.add(identifier)
            return rows
        except (TypeError, ValueError):
            raise RetrievalError("RETRIEVAL_INVALID_RESPONSE") from None

    def _hybrid_rankings(self, request, rows, *, query_vector=None, total_limit=None):
        """Return the two ranked channels and their RRF result for one scope.

        ``query_vector`` lets a structural plugin reuse the request embedding
        while re-running retrieval over a smaller, already-authorized scope.
        No source row is loaded by identifier here; callers provide the full
        allowed subset explicitly.
        """
        keyword_limit = request.budget.keyword_candidates
        vector_limit = request.budget.vector_candidates
        validate_limit(keyword_limit)
        validate_limit(vector_limit)
        total_limit = min(40, request.budget.rerank_candidates) if total_limit is None else total_limit
        validate_limit(total_limit)
        by_id = {row["chunk_id"]: row for row in rows}
        index, cache_hit = self._index(rows)
        check_deadline(request)
        keyword = self._check_ranking(index.search(request.query, set(by_id), keyword_limit), by_id, keyword_limit)
        check_deadline(request)
        embedding_calls = 0
        if query_vector is None:
            vectors = self.embedder.embed([request.query], query=True)
            embedding_calls = 1
            check_deadline(request)
            if not isinstance(vectors, (list, tuple)) or len(vectors) != 1:
                raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
            query_vector = vectors[0]
        check_deadline(request)
        dense = self._check_ranking(self.dense.search(query_vector, rows, vector_limit), by_id, vector_limit)
        check_deadline(request)
        fused = reciprocal_rank_fusion([keyword, dense], total_limit)
        return {"keyword": keyword, "dense": dense, "fused": fused,
                "query_vector": query_vector, "embedding_calls": embedding_calls,
                "bm25_cache_hit": cache_hit}

    def retrieve(self, request, chunks):
        check_deadline(request)
        rows = validate_chunks(chunks)
        keyword_limit = request.budget.keyword_candidates
        vector_limit = request.budget.vector_candidates
        total_limit = min(40, request.budget.rerank_candidates)
        trace = {"plugin": self.name, "keyword_count": 0, "vector_count": 0, "bm25_cache_hit": False,
                 'embedding_calls': 0,
                 "budgets": {"keyword": keyword_limit, "vector": vector_limit, "rerank": total_limit}}
        if not rows:
            return {"candidates": [], "trace": trace}
        by_id = {row["chunk_id"]: row for row in rows}
        ranked = self._hybrid_rankings(request, rows, total_limit=total_limit)
        keyword, dense, fused = ranked["keyword"], ranked["dense"], ranked["fused"]
        trace["bm25_cache_hit"] = ranked["bm25_cache_hit"]
        trace['embedding_calls'] = ranked["embedding_calls"]
        trace['embedding_usage'] = getattr(self.embedder, 'last_usage', None)
        candidates = [Candidate(chunk_id=identifier, retrieval_version_id=by_id[identifier]["retrieval_version_id"],
            material_version_id=by_id[identifier]["material_version_id"], parent_id=by_id[identifier].get("parent_id"),
            channel="hybrid", rank=rank, score=score).model_dump() for rank, (identifier, score) in enumerate(fused, 1)]
        trace.update(keyword_count=len(keyword), vector_count=len(dense), fused_count=len(candidates))
        check_deadline(request)
        return {"candidates": candidates, "trace": trace}
