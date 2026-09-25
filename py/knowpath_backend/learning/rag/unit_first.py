"""B3 unit-first retrieval over authorized continuation groups."""
from __future__ import annotations

from dataclasses import asdict

from .contracts import Candidate
from .plugins import OrdinaryPlugin, check_deadline, validate_chunks
from .retrieval import RetrievalError, valid_vector
from .units import SemanticUnit, build_units


class UnitFirstPlugin(OrdinaryPlugin):
    """Admit complete semantic units while keeping leaf citations authoritative."""

    name = "b3_unit"

    def __init__(self, embedder, dense, *, unit_dense=None, **kwargs):
        super().__init__(embedder, dense, **kwargs)
        self.unit_dense = unit_dense

    @staticmethod
    def _authorized_units(rows, units, tree_version_id=None):
        """Supplied records must match units rebuilt from authoritative leaves."""
        try:
            canonical = {unit.anchor_id: unit for unit in
                         build_units(rows, tree_version_id=tree_version_id)}
            if units is None:
                return canonical
            values = units.items() if isinstance(units, dict) else (
                (getattr(unit, "anchor_id", None), unit) for unit in units)
            result = {}
            for key, value in values:
                if not isinstance(key, str) or key not in canonical or key in result:
                    raise ValueError()
                expected = canonical[key]
                if isinstance(value, SemanticUnit):
                    if value != expected:
                        raise ValueError()
                elif isinstance(value, dict):
                    if (not isinstance(value.get("leaf_ids"), (list, tuple))
                            or tuple(value["leaf_ids"]) != expected.leaf_ids
                            or value.get("retrieval_text") != expected.retrieval_text):
                        raise ValueError()
                    fields = asdict(expected)
                    for name, supplied in value.items():
                        if name not in fields:
                            raise ValueError()
                        if name in {"leaf_ids", "source_spans"}:
                            supplied = tuple(supplied)
                        if supplied != fields[name]:
                            raise ValueError()
                else:
                    raise ValueError()
                result[key] = expected
            return result
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RetrievalError("UNIT_SOURCE_INVALID") from None

    @staticmethod
    def _unit_identity(unit):
        return {"unit_id": unit.unit_id, "anchor_id": unit.anchor_id,
                "leaf_ids": list(unit.leaf_ids), "source_map_hash": unit.source_map_hash,
                "retrieval_version_id": unit.retrieval_version_id,
                "material_version_id": unit.material_version_id,
                "tree_version_id": unit.tree_version_id, "parent_id": unit.parent_id}

    def retrieve(self, request, chunks, *, units=None, tree_version_id=None):
        check_deadline(request)
        rows = validate_chunks(chunks)
        by_id = {row["chunk_id"]: row for row in rows}
        limit = min(40, request.budget.rerank_candidates)
        trace = {"plugin": self.name, "rerank_mode": "unit_atomic", "embedding_calls": 0,
                 "unit_hits": [], "unit_expansions": [], "skipped_units": [],
                 "unit_decisions": [], "rerank_groups": [], "fallback_reason": None,
                 "baseline_candidate_ids": [], "baseline_candidate_sources": [],
                 "structural_additions": 0, "a_retention_at_40": None,
                 "generation_source": "leaf", "budgets": {"rerank": limit}}
        if not rows:
            return {"candidates": [], "trace": trace}

        # A is computed once outside the structural fallback boundary. Without
        # its one embedding and fused ranking there is no valid fallback.
        vectors = self.embedder.embed([request.query], query=True)
        check_deadline(request)
        if (not isinstance(vectors, (list, tuple)) or len(vectors) != 1
                or not valid_vector(vectors[0], self.embedder.dimension)):
            raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
        query_vector = vectors[0]
        trace["embedding_calls"] = 1
        trace["embedding_usage"] = getattr(self.embedder, "last_usage", None)
        ranked = self._hybrid_rankings(request, rows, query_vector=query_vector, total_limit=limit)
        check_deadline(request)
        fused = self._check_ranking(ranked["fused"], by_id, limit)
        base = [self._candidate(by_id[identifier], rank, "hybrid", score)
                for rank, (identifier, score) in enumerate(fused, 1)]
        seed_ids = [identifier for identifier, _ in fused]
        seed_ranks = {identifier: rank for rank, identifier in enumerate(seed_ids, 1)}
        trace.update(baseline_candidate_ids=seed_ids,
                     baseline_candidate_sources=[{"chunk_id": identifier,
                                                   "source_spans": by_id[identifier]["source_spans"]}
                                                  for identifier in seed_ids],
                     keyword_count=len(ranked["keyword"]), vector_count=len(ranked["dense"]),
                     fused_count=len(base), bm25_cache_hit=ranked["bm25_cache_hit"],
                     a_retention_at_40=1.0 if seed_ids else None)

        try:
            units_by_anchor = self._authorized_units(rows, units, tree_version_id)
            check_deadline(request)
            if units is None:
                if self.unit_dense is None:
                    raise RetrievalError("UNIT_INDEX_NOT_READY")
                unit_rows = [self._unit_row(unit) for unit in units_by_anchor.values()]
                unit_limit = min(request.budget.vector_candidates, limit)
                by_unit_id = {unit.unit_id: unit.anchor_id for unit in units_by_anchor.values()}
                try:
                    unit_ranked = self.unit_dense.search(query_vector, unit_rows, limit=unit_limit)
                    check_deadline(request)
                    unit_ranked = self._check_ranking(unit_ranked, by_unit_id, unit_limit)
                except RetrievalError as error:
                    if error.code in {"RETRIEVAL_INVALID_RESPONSE", "VECTOR_INVALID_RESPONSE"}:
                        raise RetrievalError("UNIT_INDEX_INVALID_RESPONSE") from None
                    raise
                unit_hits = {by_unit_id[identifier] for identifier, _ in unit_ranked}
            else:
                # Injected hits are a test seam, never an authorization seam.
                unit_hits = set(units_by_anchor)
        except Exception as error:
            check_deadline(request)
            code = getattr(error, "code", None)
            if isinstance(error, TimeoutError) or code in {
                    "RETRIEVAL_DEADLINE_EXCEEDED", "RAG_DEADLINE_EXCEEDED",
                    "RAG_COST_BUDGET_EXCEEDED", "RAG_SPEND_CONFIG_INVALID", "RATE_LIMITED"}:
                raise
            trace["fallback_reason"] = code if code in {
                "UNIT_SOURCE_INVALID", "UNIT_INDEX_NOT_READY", "UNIT_INDEX_INVALID_RESPONSE"
            } else "UNIT_INDEX_NOT_READY"
            trace["rerank_mode"] = "leaf"
            trace["rerank_groups"] = []
            return {"candidates": base, "trace": trace}

        trace["unit_hits"] = sorted(unit_hits)
        trace["structure_version_ids"] = sorted({unit.tree_version_id for unit in units_by_anchor.values()})
        leaf_to_anchor = {identifier: anchor for anchor, unit in units_by_anchor.items()
                          for identifier in unit.leaf_ids}
        selected, selected_ids, handled = [], set(), set()
        for seed_id in seed_ids:
            check_deadline(request)
            anchor = leaf_to_anchor.get(seed_id)
            unit = units_by_anchor.get(anchor)
            if unit is not None and anchor not in handled:
                handled.add(anchor)
                decision = {**self._unit_identity(unit), "seed_id": seed_id,
                            "seed_rank": seed_ranks[seed_id], "admitted": False}
                missing = [identifier for identifier in unit.leaf_ids if identifier not in selected_ids]
                if anchor not in unit_hits:
                    decision["reason"] = "unit_not_hit"
                elif len(selected) + len(missing) > limit:
                    decision["reason"] = "candidate_budget"
                else:
                    selected.extend(missing)
                    selected_ids.update(missing)
                    decision.update(admitted=True, reason=None)
                    trace["unit_expansions"].append(dict(decision))
                    trace["rerank_groups"].append({"group_id": anchor,
                                                   **self._unit_identity(unit),
                                                   "retrieval_text": unit.retrieval_text})
                trace["unit_decisions"].append(decision)
                if not decision["admitted"]:
                    trace["skipped_units"].append(dict(decision))
            if seed_id not in selected_ids and len(selected) < limit:
                selected.append(seed_id)
                selected_ids.add(seed_id)
                trace["rerank_groups"].append({"group_id": seed_id, "leaf_ids": [seed_id],
                                               "retrieval_text": by_id[seed_id]["retrieval_text"]})

        for anchor in sorted(unit_hits - handled):
            decision = {**self._unit_identity(units_by_anchor[anchor]), "seed_id": None,
                        "seed_rank": None, "admitted": False, "reason": "no_a_seed"}
            trace["unit_decisions"].append(decision)
            trace["skipped_units"].append(dict(decision))
        candidates = [self._candidate(by_id[identifier], rank, "tree", None)
                      for rank, identifier in enumerate(selected, 1)]
        trace["structural_additions"] = len(selected_ids - set(seed_ids))
        trace["a_retention_at_40"] = len(set(seed_ids) & selected_ids) / len(seed_ids) if seed_ids else None
        check_deadline(request)
        return {"candidates": candidates, "trace": trace}

    @staticmethod
    def _candidate(row, rank, channel, score):
        return Candidate(chunk_id=row["chunk_id"], retrieval_version_id=row["retrieval_version_id"],
                         material_version_id=row["material_version_id"], parent_id=row.get("parent_id"),
                         channel=channel, rank=rank, score=score).model_dump()

    @staticmethod
    def _unit_row(unit):
        return {"chunk_id": unit.unit_id,
                "material_version_id": unit.material_version_id,
                "retrieval_version_id": unit.retrieval_version_id,
                "source_text": unit.source_text,
                "retrieval_text": unit.retrieval_text,
                "parent_id": unit.parent_id,
                "ordinal": unit.ordinal,
                "source_spans": list(unit.source_spans),
                "quality": {"token_count": unit.token_count},
                "tree_version_id": unit.tree_version_id,
                "source_map_hash": unit.source_map_hash,
                "leaf_ids": list(unit.leaf_ids), "anchor_id": unit.anchor_id}
