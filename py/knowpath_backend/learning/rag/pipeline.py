"""One pinned scope and one shared answering path for A and B1."""
from __future__ import annotations

import copy
import time

from .context import assemble_context
from .contracts import Candidate, Citation, RetrievalBudget, RetrievalRequest, SourceSpan
from .lifecycle import ManifestLifecycle
from .scope import covers, legacy_span
from .verification import VerificationError
from .queries import prepare_query
from .plugins import OrdinaryPlugin
from .registry import create_plugin
from ..errors import DomainConflict, DomainNotFound
from ..persistence.rag_repository import RagIntegrityError, RagConflict


class RagPipeline:
    def __init__(self, repository, materials, spaces, plugin, reranker, verifier, *,
                 budget=None, timeout_seconds=120, require_b1=False):
        self.repo, self.materials, self.spaces = repository, materials, spaces
        self.plugin, self.reranker, self.verifier = plugin, reranker, verifier
        self.budget = budget or RetrievalBudget()
        self.timeout_seconds, self.require_b1 = timeout_seconds, require_b1
        self.lifecycle = ManifestLifecycle(repository)

    def _scope(self, space_id):
        try:
            return self.spaces.rag_scope_snapshot(space_id)
        except (DomainConflict, DomainNotFound):
            raise VerificationError("STALE_LEARNING_CONTEXT") from None

    def _guard(self, scope, deadline, cancelled):
        if time.monotonic() >= deadline:
            raise VerificationError("RAG_DEADLINE_EXCEEDED")
        if cancelled is not None and cancelled():
            raise VerificationError("RAG_CANCELLED")
        if self._scope(scope.space_id).scope_snapshot_id != scope.scope_snapshot_id:
            raise VerificationError("STALE_LEARNING_CONTEXT")

    def _originals(self, scope, manifests):
        """Reconstruct every candidate from authoritative original SQL chunks.

        Neither stored retrieval text nor a plugin response can grant additional
        original ranges. Titles are the frozen deterministic indexing prefix.
        """
        versions, rows = {}, []
        for manifest in manifests:
            rv = manifest["retrieval_version_id"]
            # Establish dependency identities BEFORE authorization/retrieval
            # filtering. A missing or excluded continuation cannot disappear
            # from the definition of a complete semantic unit.
            groups = {}
            for full in self.repo.list_chunks(rv):
                group = full.get("continuation_of") or full["chunk_id"]
                groups.setdefault(group, set()).add(full["chunk_id"])
            for row in self.repo.list_scope_chunks(scope.scope_snapshot_id, rv):
                if row.get('quality', {}).get('content_status') == 'damaged':
                    continue
                version_id = row["material_version_id"]
                if version_id not in versions:
                    version = self.materials.get_version(version_id)
                    if version is None:
                        raise VerificationError("STALE_LEARNING_CONTEXT")
                    versions[version_id] = (version, {c.id: c for c in version.chunks})
                version, originals = versions[version_id]
                spans = tuple(SourceSpan.model_validate(s) for s in row["source_spans"])
                if not spans or any(not covers(s, scope.allowed_spans) for s in spans):
                    raise VerificationError("RAG_SOURCE_INVALID")
                parts = []
                for span in spans:
                    original = originals.get(span.block)
                    if original is None or not covers(span, [legacy_span(version_id, original)]):
                        raise VerificationError("RAG_SOURCE_INVALID")
                    parts.append(original.text[span.start:span.end])
                text = "\n".join(parts)
                if row["source_text"] != text:
                    raise VerificationError("RAG_SOURCE_INVALID")
                rows.append({**copy.deepcopy(row), "material_id": version.material_id, 'citation_schema_version': 2,
                             "source_text": text, "text": text,
                             "evidence_group": row.get("continuation_of") or row["chunk_id"],
                             "requires": sorted(set(row.get("requires", [])) | (
                                 groups[row.get("continuation_of") or row["chunk_id"]] - {row["chunk_id"]}))})
        if len({r["chunk_id"] for r in rows}) != len(rows):
            raise VerificationError("RAG_SOURCE_INVALID")
        return rows

    def answer(self, question, *, space_id, expected_scope_version=None, expected_bindings=None, cancelled=None, history=()):
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        scope = self._scope(space_id)
        if expected_scope_version is not None and scope.scope_version != expected_scope_version:
            raise VerificationError("STALE_LEARNING_CONTEXT")
        if expected_bindings is not None:
            identity = lambda bindings: sorted((b["material_id"], b["material_version_id"], b["graph_version"]) for b in bindings)
            if identity(expected_bindings) != identity([b.model_dump() for b in scope.bindings]):
                raise VerificationError("STALE_LEARNING_CONTEXT")
        self._guard(scope, deadline, cancelled)
        try:
            manifests = self.lifecycle.pin(scope, require_b1=self.require_b1)
        except (RagIntegrityError, RagConflict):
            raise VerificationError("RAG_INDEX_NOT_READY") from None
        plugin = self.plugin
        if isinstance(plugin, OrdinaryPlugin):
            implementation = plugin
            plugin = create_plugin('b1' if self.require_b1 else 'a', plugin.embedder, plugin.dense,
                bm25_profile=plugin.bm25_profile)
            plugin.implementation = implementation
        if hasattr(plugin, 'validate_runtime'):
            try:
                plugin.validate_runtime(manifests)
            except ValueError:
                raise VerificationError('VECTOR_PROFILE_MISMATCH') from None
        prepared = prepare_query(question, history)
        if prepared['status'] == 'clarify':
            self._guard(scope, deadline, cancelled)
            return {'status': 'clarify', 'text': '请明确本轮问题指向的对象或资料范围。', 'citations': [],
                'citation_ids': [], 'claims': [], 'sources': [], 'trace': {'query': prepared,
                'scope_snapshot_id': scope.scope_snapshot_id, 'manifest_ids': [m['manifest_id'] for m in manifests],
                'retrieval_versions': [m['retrieval_version_id'] for m in manifests], 'generation_calls': 0,
                'verification_calls': 0, 'revisions': 0, 'elapsed_seconds': time.monotonic()-started}}
        resolved_question = prepared['query']
        request = RetrievalRequest(query=resolved_question, original_query=question, scope_snapshot_id=scope.scope_snapshot_id,
            manifest_ids=tuple(m["manifest_id"] for m in manifests), budget=self.budget, deadline=deadline)
        rows = self._originals(scope, manifests)
        self._guard(scope, deadline, cancelled)
        if hasattr(plugin, 'for_sources'):
            plugin = plugin.for_sources(rows)
        retrieved = plugin.retrieve(request)
        self._guard(scope, deadline, cancelled)
        if retrieved.status != 'ready':
            raise VerificationError(retrieved.error_code or 'PLUGIN_RETRIEVAL_FAILED')
        retrieval_trace = getattr(plugin, 'last_trace', None) or {'trace_id': retrieved.trace_id}
        candidates = [Candidate.model_validate(c) for c in retrieved.candidates]
        by_id = {r["chunk_id"]: r for r in rows}
        if len(candidates) > self.budget.rerank_candidates or len({c.chunk_id for c in candidates}) != len(candidates):
            raise VerificationError("RAG_CANDIDATES_INVALID")
        for c in candidates:
            original = by_id.get(c.chunk_id)
            if original is None or (c.material_version_id, c.retrieval_version_id) != (
                original["material_version_id"], original["retrieval_version_id"]):
                raise VerificationError("RAG_CANDIDATES_INVALID")
        selected = [by_id[c.chunk_id] for c in candidates]
        ranked = self.reranker.rerank(resolved_question, selected, deadline=deadline) if selected else []
        self._guard(scope, deadline, cancelled)
        if len(ranked) != len(selected) or {r["chunk_id"] for r in ranked} != {r["chunk_id"] for r in selected}:
            raise VerificationError("RAG_RERANK_INVALID")
        # Reranker controls ordering only. Restore authoritative source rows.
        context = assemble_context([by_id[r["chunk_id"]] for r in ranked], max_tokens=self.budget.context_tokens)
        result = self.verifier.answer(resolved_question, context, deadline=deadline,
            max_generation_calls=self.budget.max_generation_calls,
            max_verification_calls=self.budget.max_verification_calls)
        self._guard(scope, deadline, cancelled)
        citations = []
        by_context = {s['chunk_id']:s for s in context}
        for verdict in result['trace']['verdicts']:
            for check in verdict['checks']:
                check['source_spans'] = [span for cid in check['citation_ids']
                    for span in by_context[cid]['source_spans']]
            for point in verdict.get('requirement_checks') or []:
                point['source_spans'] = [span for cid in point['citation_ids']
                    for span in by_context[cid]['source_spans']]
        for identifier in result["citation_ids"]:
            source = next((s for s in context if s["chunk_id"] == identifier), None)
            if source is None:
                raise VerificationError("RAG_SOURCE_INVALID")
            citations.append(Citation(material_id=source["material_id"], material_version_id=source["material_version_id"],
                retrieval_version_id=source["retrieval_version_id"], chunk_id=identifier,
                source_spans=source["source_spans"]).model_dump(mode="json"))
        return {**result, "citations": citations, "sources": context,
                "trace": {**result["trace"], "retrieval": retrieval_trace, 'query': prepared,
                    'rerank_usage': getattr(self.reranker, 'last_usage', None),
                    'rerank_calls': 1 if selected else 0,
                    'embedding_usage': retrieval_trace.get('embedding_usage'),
                    'candidate_ids': [c.chunk_id for c in candidates],
                    'candidate_sources': [{'chunk_id': c.chunk_id, 'source_spans': by_id[c.chunk_id]['source_spans']}
                        for c in candidates],
                    'reranked_sources': [{'chunk_id': r['chunk_id'], 'source_spans': by_id[r['chunk_id']]['source_spans']}
                        for r in ranked],
                    "scope_snapshot_id": scope.scope_snapshot_id, "manifest_ids": list(request.manifest_ids),
                    "retrieval_versions": [m["retrieval_version_id"] for m in manifests],
                    "reranked_ids": [r["chunk_id"] for r in ranked], "context_ids": [r["chunk_id"] for r in context],
                    "elapsed_seconds": time.monotonic() - started}}
