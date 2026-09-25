"""B1.5 section coarse-to-fine retrieval with a flat-RAG safety path.

The plugin never retrieves a section or summary as evidence. Sections are only
an online routing index over the caller-provided, already-authorized leaf rows.
Global leaf retrieval contributes a channel to the final equal-weight RRF; the
hard candidate cap means a focused result may reorder or displace a global row.
If navigation fails, the exact flat global result is returned instead.
"""
from __future__ import annotations

from collections import defaultdict

from .bm25 import tokenize
from .contracts import Candidate
from .fusion import reciprocal_rank_fusion
from .plugins import OrdinaryPlugin, check_deadline, validate_chunks
from .retrieval import RetrievalError


def section_key(row):
    path = row.get("section_path")
    if (not isinstance(path, (list, tuple)) or not path
            or any(type(part) is not str or not part.strip() for part in path)):
        return None
    retrieval_version_id = row.get("retrieval_version_id")
    material_version_id = row.get("material_version_id")
    if (type(retrieval_version_id) is not str or not retrieval_version_id
            or type(material_version_id) is not str or not material_version_id):
        return None
    quality = row.get("quality")
    # A section is a routing index only when its structure quality is explicitly
    # known. Missing quality must not silently opt a row into structural recall.
    if not isinstance(quality, dict) or quality.get("structure") != "known":
        return None
    return retrieval_version_id, material_version_id, tuple(path)


class SectionPlugin(OrdinaryPlugin):
    """Structure-aware plugin without learned or hand-tuned feature weights."""

    name = "b15"

    def __init__(self, *args, section_limit=3, children_per_section=3, **kwargs):
        super().__init__(*args, **kwargs)
        if type(section_limit) is not int or not 1 <= section_limit <= 3:
            raise ValueError("section limit must be between 1 and 3")
        if type(children_per_section) is not int or not 1 <= children_per_section <= 3:
            raise ValueError("children per section must be between 1 and 3")
        self.section_limit = section_limit
        self.children_per_section = children_per_section

    @staticmethod
    def _round_robin(local_ids, by_section, cap):
        result = []
        for offset in range(cap):
            for key in local_ids:
                values = by_section.get(key, ())
                if offset < len(values):
                    result.append(values[offset])
        return result

    def _selected_sections(self, request, rows, fused, by_id):
        grouped = defaultdict(list)
        for rank, (identifier, _) in enumerate(fused, 1):
            key = section_key(by_id[identifier])
            if key is not None:
                grouped[key].append(rank)
        query_terms = set(tokenize(request.query))
        eligible = []
        for key, ranks in grouped.items():
            path = key[2]
            path_hit = bool(query_terms.intersection(tokenize(" ".join(path))))
            if len(ranks) >= 2 or path_hit:
                eligible.append((key, min(ranks), len(ranks), path_hit))
        # This is a deterministic gate, not a learned score: path hits first,
        # then earliest global rank, then consensus count and stable path.
        eligible.sort(key=lambda item: (not item[3], item[1], -item[2], item[0]))
        return eligible[:self.section_limit]

    def retrieve(self, request, chunks):
        check_deadline(request)
        rows = validate_chunks(chunks)
        total_limit = min(40, request.budget.rerank_candidates)
        trace = {
            "plugin": self.name,
            "keyword_count": 0,
            "vector_count": 0,
            "bm25_cache_hit": False,
            "embedding_calls": 0,
            "budgets": {
                "keyword": request.budget.keyword_candidates,
                "vector": request.budget.vector_candidates,
                "rerank": total_limit,
                "sections": self.section_limit,
                "children_per_section": self.children_per_section,
            },
            "fallback": False,
            "fallback_reason": None,
            "navigation_error": None,
            "section_candidates": [],
            "selected_sections": [],
            "focused_children": [],
            "focused_keyword_count": 0,
            "focused_vector_count": 0,
            "seeds": [],
            "extensions": [],
        }
        if not rows:
            trace["fallback"] = True
            trace["fallback_reason"] = "empty_scope"
            return {"candidates": [], "trace": trace}

        by_id = {row["chunk_id"]: row for row in rows}
        global_ranked = self._hybrid_rankings(request, rows, total_limit=total_limit)
        trace.update(keyword_count=len(global_ranked["keyword"]),
                     vector_count=len(global_ranked["dense"]),
                     fused_count=len(global_ranked["fused"]),
                     bm25_cache_hit=global_ranked["bm25_cache_hit"],
                     embedding_calls=global_ranked["embedding_calls"],
                     embedding_usage=getattr(self.embedder, "last_usage", None))
        global_ids = [identifier for identifier, _ in global_ranked["fused"]]
        trace["seeds"] = list(global_ids)
        selected = self._selected_sections(request, rows, global_ranked["fused"], by_id)
        check_deadline(request)
        trace["section_candidates"] = [
            {"section_path": list(item[0][2]), "first_rank": item[1],
             "seed_count": item[2], "path_hit": item[3],
             "retrieval_version_id": item[0][0],
             "material_version_id": item[0][1]}
            for item in selected
        ]
        if not selected:
            trace["fallback"] = True
            trace["fallback_reason"] = "no_confident_section"
            final = global_ranked["fused"]
            local_ids = set()
        else:
            selected_keys = [item[0] for item in selected]
            selected_set = set(selected_keys)
            focused = [row for row in rows if section_key(row) in selected_set]
            trace["selected_sections"] = [
                {"retrieval_version_id": key[0], "material_version_id": key[1],
                 "section_path": list(key[2])}
                for key in selected_keys
            ]
            trace["focused_children"] = [row["chunk_id"] for row in focused]
            check_deadline(request)
            try:
                local_ranked = self._hybrid_rankings(
                    request, focused, query_vector=global_ranked["query_vector"],
                    total_limit=min(total_limit, len(focused)))
            except RetrievalError as error:
                if error.code == "RETRIEVAL_DEADLINE_EXCEEDED":
                    raise
                if error.code not in {"VECTOR_UNAVAILABLE", "VECTOR_INDEX_NOT_READY"}:
                    raise
                trace["fallback"] = True
                trace["fallback_reason"] = "focused_retrieval_failed"
                # Keep service diagnostics coarse. Provider/index error codes
                # can expose deployment details through a user-visible trace.
                trace["navigation_error"] = "service_unavailable"
                final = global_ranked["fused"]
                local_ids = set()
            else:
                trace.update(focused_keyword_count=len(local_ranked["keyword"]),
                             focused_vector_count=len(local_ranked["dense"]),
                             focused_bm25_cache_hit=local_ranked["bm25_cache_hit"])
                local_by_section = defaultdict(list)
                for identifier, _ in local_ranked["fused"]:
                    local_by_section[section_key(by_id[identifier])].append(identifier)
                local_ids_ordered = self._round_robin(selected_keys, local_by_section,
                                                       self.children_per_section)
                local_ids = set(local_ids_ordered)
                final = reciprocal_rank_fusion(
                    [[identifier for identifier, _ in global_ranked["fused"]], local_ids_ordered],
                    total_limit)
                check_deadline(request)

        global_id_set = set(global_ids)
        final_ids = [identifier for identifier, _ in final]
        extension_ids = [identifier for identifier in final_ids if identifier not in global_id_set]
        trace["extensions"] = [
            {"chunk_id": identifier, "retrieval_version_id": by_id[identifier]["retrieval_version_id"],
             "material_version_id": by_id[identifier]["material_version_id"],
             "parent_id": by_id[identifier].get("parent_id"), "reason": "section_focus"}
            for identifier in extension_ids
        ]
        candidates = [Candidate(
            chunk_id=identifier,
            retrieval_version_id=by_id[identifier]["retrieval_version_id"],
            material_version_id=by_id[identifier]["material_version_id"],
            parent_id=by_id[identifier].get("parent_id"),
            channel="tree" if identifier in local_ids else "hybrid",
            rank=rank,
            score=score,
        ).model_dump() for rank, (identifier, score) in enumerate(final, 1)]
        trace["extra_read_count"] = len(extension_ids)
        check_deadline(request)
        return {"candidates": candidates, "trace": trace}
