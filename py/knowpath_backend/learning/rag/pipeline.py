"""One pinned scope and one shared answering path for A and B1."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .context import assemble_context, explicit_anchor_rows, anchor_repacking_plan
from .contracts import Candidate, Citation, RetrievalBudget, RetrievalRequest, SourceSpan
from .lifecycle import ManifestLifecycle
from .scope import covers, legacy_span
from .verification import VerificationError
from .queries import prepare_query
from .plugins import OrdinaryPlugin
from .registry import create_plugin
from .units import build_unit
from ..errors import DomainConflict, DomainNotFound
from ..persistence.rag_repository import RagIntegrityError, RagConflict


def select_context_with_anchors(capacity, question, rows, *, max_evidence_tokens):
    """Pack once, then try at most one guarded rescue under the same limits."""
    rows = list(rows)
    context, capacity_trace = capacity.select_context(question, rows,
        max_evidence_tokens=max_evidence_tokens)
    candidate_order, decision = anchor_repacking_plan(question, rows, context,
        omitted_chunk_ids=(capacity_trace or {}).get('omitted_chunk_ids', ()),
        invalid_chunk_ids=(capacity_trace or {}).get('invalid_chunk_ids', ()))
    if decision['reason'] is not None:
        return context, capacity_trace, decision

    decision['attempted'] = True
    try:
        candidate_context, candidate_trace = capacity.select_context(question, candidate_order,
            max_evidence_tokens=max_evidence_tokens)
    except VerificationError as error:
        if error.code != 'MODEL_TOKEN_BUDGET_EXCEEDED':
            raise
        decision['reason'] = 'capacity_rejected'
    else:
        candidate_ids = [row['chunk_id'] for row in candidate_context]
        candidate_set = set(candidate_ids)
        protected = decision['protected_chunk_ids']
        existing_anchors = {row['chunk_id'] for row in explicit_anchor_rows(question, context)}
        added = [identifier for identifier in decision['missing_anchor_ids'] if identifier in candidate_set]
        if candidate_ids[:len(protected)] != protected:
            decision['reason'] = 'protected_context_not_retained'
        elif not existing_anchors <= candidate_set:
            decision['reason'] = 'existing_anchor_not_retained'
        elif not added:
            decision['reason'] = 'capacity_rejected'
        else:
            decision.update(reason='accepted', added_anchor_ids=added)
            return candidate_context, candidate_trace, decision
    # AnswerCapacity exposes the last selection for diagnostics. A rejected
    # attempt must not replace either the selected rows or their capacity trace.
    capacity.last_trace = capacity_trace
    return context, capacity_trace, decision


def expand_atomic_rerank_groups(ranked_groups, by_id):
    """Expand validated unit rerank groups into ordered authorized leaf rows."""
    ordered, seen = [], set()
    for group in ranked_groups:
        leaf_ids = group.get("leaf_ids") if isinstance(group, dict) else None
        if not isinstance(leaf_ids, (list, tuple)) or not leaf_ids:
            raise VerificationError("RAG_RERANK_INVALID")
        for identifier in leaf_ids:
            if not isinstance(identifier, str) or identifier in seen or identifier not in by_id:
                raise VerificationError("RAG_RERANK_INVALID")
            seen.add(identifier)
            ordered.append(by_id[identifier])
    if seen != set(by_id):
        raise VerificationError("RAG_RERANK_INVALID")
    return ordered


def authoritative_rerank_groups(groups, selected, authorized):
    """Trace groups describe ordering; only selected SQL leaves grant evidence."""
    if not isinstance(groups, list):
        raise VerificationError("RAG_RERANK_INVALID")
    candidate_rows = {row["chunk_id"]: row for row in selected}
    expand_atomic_rerank_groups(groups, candidate_rows)
    rebuilt, group_ids = [], set()
    for group in groups:
        group_id = group.get("group_id")
        leaf_ids = group["leaf_ids"]
        if not isinstance(group_id, str) or not group_id or group_id in group_ids:
            raise VerificationError("RAG_RERANK_INVALID")
        group_ids.add(group_id)
        members = [candidate_rows[identifier] for identifier in leaf_ids]
        if group.get('kind') == 'packet':
            from .evidence_packets import build_packets
            packets = {p.packet_id: p for p in build_packets(authorized.values()).packets}
            packet = packets.get(group_id)
            if packet is None or tuple(leaf_ids) != packet.leaf_ids:
                raise VerificationError('RAG_RERANK_INVALID')
            text = packet.source_text
        elif len(members) == 1:
            if group_id != leaf_ids[0]:
                raise VerificationError("RAG_RERANK_INVALID")
            text = members[0]["retrieval_text"]
        else:
            try:
                unit = build_unit(members, tree_version_id=members[0].get("tree_version_id"))
                complete_ids = {row["chunk_id"] for row in authorized.values()
                                if (row.get("continuation_of") or row["chunk_id"]) == unit.anchor_id}
                if (unit.anchor_id != group_id or tuple(leaf_ids) != unit.leaf_ids
                        or complete_ids != set(unit.leaf_ids)):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise VerificationError("RAG_RERANK_INVALID") from None
            text = unit.retrieval_text
        rebuilt.append({"chunk_id": group_id, "group_id": group_id,
                        "retrieval_text": text, "leaf_ids": list(leaf_ids), 'kind': group.get('kind')})
    return rebuilt


class RagPipeline:
    def __init__(self, repository, materials, spaces, plugin, reranker, verifier, *,
                 budget=None, timeout_seconds=120, require_b1=False):
        self.repo, self.materials, self.spaces = repository, materials, spaces
        self.plugin, self.reranker, self.verifier = plugin, reranker, verifier
        self.budget = budget or RetrievalBudget()
        self.timeout_seconds, self.require_b1 = timeout_seconds, require_b1
        self.lifecycle = ManifestLifecycle(repository)
        self.last_retrieval_snapshot = None
        self.retrieval_snapshot_callback = None

    def _snapshot(self, **fields):
        snapshot = {**(self.last_retrieval_snapshot or {}), **copy.deepcopy(fields)}
        self.last_retrieval_snapshot = snapshot
        if self.retrieval_snapshot_callback is not None:
            self.retrieval_snapshot_callback(copy.deepcopy(snapshot))

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
                rows.append({**copy.deepcopy(row), "material_id": version.material_id,
                             "tree_version_id": manifest.get("tree_version_id"), 'citation_schema_version': 2,
                             "source_text": text, "text": text,
                             "evidence_group": row.get("continuation_of") or row["chunk_id"],
                             "requires": sorted(set(row.get("requires", [])) | (
                                 groups[row.get("continuation_of") or row["chunk_id"]] - {row["chunk_id"]}))})
        if len({r["chunk_id"] for r in rows}) != len(rows):
            raise VerificationError("RAG_SOURCE_INVALID")
        return rows

    def answer(self, question, *, space_id, expected_scope_version=None, expected_bindings=None, cancelled=None, history=()):
        self.last_retrieval_snapshot = None
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
            plugin_name = getattr(plugin, 'name', None)
            mode = plugin_name if plugin_name in {'a', 'a0', 'f', 'b4', 'a_large', 'b1', 'b15', 'b2_r1', 'b3_unit'} else (
                'b1' if self.require_b1 else 'a')
            plugin = create_plugin(mode, plugin.embedder, plugin.dense,
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
        self._snapshot(stage='candidate', scope_snapshot_id=scope.scope_snapshot_id,
            manifest_ids=list(request.manifest_ids), retrieval_versions=[m['retrieval_version_id'] for m in manifests],
            candidate_ids=[c.chunk_id for c in candidates], retrieval=retrieval_trace,
            candidate_sources=[{'chunk_id': r['chunk_id'], 'source_spans': r['source_spans']} for r in selected])
        atomic_groups = retrieval_trace.get("rerank_groups")
        if retrieval_trace.get("rerank_mode") == "unit_atomic" or atomic_groups:
            group_rows = authoritative_rerank_groups(atomic_groups, selected, by_id)
            by_group = {row["chunk_id"]: row for row in group_rows}
            ranked_groups = self.reranker.rerank(resolved_question, copy.deepcopy(group_rows), deadline=deadline) if group_rows else []
            self._guard(scope, deadline, cancelled)
            if (not isinstance(ranked_groups, (list, tuple)) or len(ranked_groups) != len(group_rows)
                    or any(not isinstance(row, dict) or not isinstance(row.get("chunk_id"), str)
                           for row in ranked_groups)
                    or {r["chunk_id"] for r in ranked_groups} != set(by_group)):
                raise VerificationError("RAG_RERANK_INVALID")
            ordered = expand_atomic_rerank_groups([by_group[row["chunk_id"]] for row in ranked_groups],
                                                 {row["chunk_id"]: row for row in selected})
            ranked_leaf_rows = ordered
            if retrieval_trace.get('rerank_mode') == 'packet_atomic':
                packet_membership = {i: group['group_id'] for group in group_rows if group.get('kind') == 'packet'
                                     for i in group['leaf_ids']}
                ordered = [{**r, 'atomic_group': packet_membership[r['chunk_id']]} if r['chunk_id'] in packet_membership
                           else r for r in ordered]
        else:
            ranked = self.reranker.rerank(resolved_question, selected, deadline=deadline) if selected else []
            self._guard(scope, deadline, cancelled)
            if len(ranked) != len(selected) or {r["chunk_id"] for r in ranked} != {r["chunk_id"] for r in selected}:
                raise VerificationError("RAG_RERANK_INVALID")
            # Reranker controls ordering only. Restore authoritative source rows.
            ranked_leaf_rows = [by_id[r['chunk_id']] for r in ranked]
            ordered = ranked_leaf_rows
        self._guard(scope, deadline, cancelled)
        self._snapshot(stage='rerank', reranked_ids=[r['chunk_id'] for r in ranked_leaf_rows],
            reranked_sources=[{'chunk_id': r['chunk_id'], 'source_spans': r['source_spans']} for r in ranked_leaf_rows],
            rerank_usage=getattr(self.reranker, 'last_usage', None))
        capacity_trace = None
        anchor_repacking = None
        if ordered and getattr(self.verifier, 'capacity', None) is not None:
            context, capacity_trace, anchor_repacking = select_context_with_anchors(
                self.verifier.capacity, resolved_question, ordered, max_evidence_tokens=self.budget.context_tokens)
        else:
            context = assemble_context(ordered, max_tokens=self.budget.context_tokens)
        # Persist evidence before any generation/admission can fail. Atomic
        # membership is local packing metadata, not an authoritative relation.
        context = [{k:v for k,v in r.items() if k != 'atomic_group'} for r in context]
        self._snapshot(stage='context', context_ids=[r['chunk_id'] for r in context],
            context_sources=[{'chunk_id': r['chunk_id'], 'source_spans': r['source_spans']} for r in context],
            context_hash=hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        if context and getattr(self.verifier, 'capacity', None) is not None:
            self.verifier.capacity.admit_initial(resolved_question, context, deadline=deadline)
        result = self.verifier.answer(resolved_question, context, deadline=deadline,
            max_generation_calls=self.budget.max_generation_calls,
            max_verification_calls=self.budget.max_verification_calls)
        if capacity_trace is not None:
            result['trace']['protocol_capacity'] = capacity_trace
        if anchor_repacking is not None:
            result['trace']['anchor_repacking'] = anchor_repacking
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
                    'reranked_sources': [{'chunk_id': r['chunk_id'], 'source_spans': r['source_spans']}
                        for r in ranked_leaf_rows],
                    "scope_snapshot_id": scope.scope_snapshot_id, "manifest_ids": list(request.manifest_ids),
                    "retrieval_versions": [m["retrieval_version_id"] for m in manifests],
                    "reranked_ids": [r["chunk_id"] for r in ranked_leaf_rows], "context_ids": [r["chunk_id"] for r in context],
                    "elapsed_seconds": time.monotonic() - started}}
