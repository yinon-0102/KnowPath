"""B2-R1 deterministic closure over parser-derived continuation units."""
from __future__ import annotations

from collections import OrderedDict

from .contracts import Candidate
from .plugins import OrdinaryPlugin, check_deadline, validate_chunks
from .retrieval import RetrievalError


def unit_key(row):
    """Return the immutable semantic-unit identity for an authorized leaf."""
    return row.get("continuation_of") or row["chunk_id"]


def _token_cost(row):
    quality = row.get("quality")
    if isinstance(quality, dict) and type(quality.get("token_count")) is int and quality["token_count"] >= 0:
        return quality["token_count"]
    return len(row["source_text"].encode("utf-8"))


def validate_units(rows):
    """Build deterministic continuation units from already-authorized rows."""
    groups = OrderedDict()
    # Keep chunk identities separate from unit identities. A continuation may
    # appear before its root when rows are ordered by chunk_id; treating the
    # unit key as an already-seen chunk then incorrectly rejects that valid
    # unit as a duplicate.
    chunk_versions = {}
    unit_versions = {}
    for row in validate_chunks(rows):
        try:
            identifier = row["chunk_id"]
            key = unit_key(row)
            version = (row["material_version_id"], row["retrieval_version_id"], row.get("parent_id"))
            ordinal = row["ordinal"]
            tree_version = row.get("tree_version_id")
            if not isinstance(key, str) or not key or type(ordinal) is not int or ordinal < 0:
                raise ValueError()
            if not isinstance(tree_version, str) or not tree_version:
                raise ValueError()
            if identifier in chunk_versions or (
                    key in unit_versions and unit_versions[key] != version):
                raise ValueError()
            if key in chunk_versions and chunk_versions[key] != version:
                raise ValueError()
            chunk_versions[identifier] = version
            unit_versions.setdefault(key, version)
            groups.setdefault(key, []).append(row)
        except (KeyError, TypeError, ValueError):
            raise RetrievalError("STRUCTURE_UNAVAILABLE") from None
    for key, members in groups.items():
        members.sort(key=lambda row: (row["ordinal"], row["chunk_id"]))
        if len({row.get("tree_version_id") for row in members}) != 1:
            raise RetrievalError("STRUCTURE_UNAVAILABLE")
    return groups


class ContinuationClosurePlugin(OrdinaryPlugin):
    """Close complete continuation units seeded by ordinary hybrid retrieval."""

    name = "b2_r1"

    def retrieve(self, request, chunks):
        check_deadline(request)
        rows = list(chunks)
        base = super().retrieve(request, rows)
        base_candidates = list(base["candidates"])
        base_trace = dict(base["trace"])
        base_trace.update(
            plugin=self.name,
            seeds=[candidate["chunk_id"] for candidate in base_candidates],
            closures=[],
            extensions=[],
            skipped_closures=[],
            replacements=[],
            structure_version_ids=[],
            structural_additions=0,
            a_retention_at_40=1.0 if base_candidates else None,
            structure_unavailable=None,
        )
        try:
            authorized = validate_chunks(rows)
            groups = validate_units(authorized)
        except RetrievalError as error:
            base_trace["structure_unavailable"] = error.code
            base_trace["a_retention_at_40"] = 1.0 if base_candidates else None
            return {"candidates": base_candidates, "trace": base_trace}

        by_id = {row["chunk_id"]: row for row in authorized}
        base_ids = [candidate["chunk_id"] for candidate in base_candidates]
        base_by_id = {candidate["chunk_id"]: candidate for candidate in base_candidates}
        selected = []
        selected_ids = set()
        handled_units = set()
        consumed_context = 0
        candidate_limit = min(40, request.budget.rerank_candidates)
        structure_versions = {row["tree_version_id"] for row in authorized}
        base_trace["structure_version_ids"] = sorted(structure_versions)

        def append_candidate(row, *, source=None):
            identifier = row["chunk_id"]
            if identifier in selected_ids:
                return
            candidate = dict(base_by_id.get(identifier) or Candidate(
                chunk_id=identifier,
                retrieval_version_id=row["retrieval_version_id"],
                material_version_id=row["material_version_id"],
                parent_id=row.get("parent_id"),
                channel="tree",
                rank=1,
                score=None,
            ).model_dump())
            selected_ids.add(identifier)
            selected.append(candidate)

        for seed in base_candidates:
            check_deadline(request)
            if len(selected) >= candidate_limit:
                break
            seed_id = seed["chunk_id"]
            row = by_id[seed_id]
            key = unit_key(row)
            if key in handled_units:
                continue
            members = groups[key]
            missing = [member for member in members if member["chunk_id"] not in selected_ids]
            count_needed = len(missing)
            token_needed = sum(_token_cost(member) for member in missing)
            candidate_reason = None
            if len(selected) + count_needed > candidate_limit:
                candidate_reason = "candidate_budget"
            elif consumed_context + token_needed > request.budget.context_tokens:
                candidate_reason = "context_budget"
            handled_units.add(key)
            if candidate_reason is not None:
                append_candidate(row)
                consumed_context += _token_cost(row)
                if len(members) > 1:
                    base_trace["skipped_closures"].append({"unit_id": key, "reason": candidate_reason})
                continue
            for member in members:
                append_candidate(member)
                consumed_context += _token_cost(member)
                if member["chunk_id"] != seed_id and len(members) > 1:
                    entry = {
                        "unit_id": key,
                        "seed_id": seed_id,
                        "chunk_id": member["chunk_id"],
                        "edge_type": "continuation",
                        "material_version_id": member["material_version_id"],
                        "retrieval_version_id": member["retrieval_version_id"],
                        "tree_version_id": member["tree_version_id"],
                    }
                    base_trace["closures"].append(entry)
                    base_trace["extensions"].append(entry)

        for candidate in base_candidates:
            if len(selected) >= candidate_limit:
                break
            append_candidate(by_id[candidate["chunk_id"]])

        selected = [dict(candidate, rank=index) for index, candidate in enumerate(selected, 1)]
        selected_ids = {candidate["chunk_id"] for candidate in selected}
        base_trace["replacements"] = [
            {"chunk_id": identifier, "reason": "candidate_capacity"}
            for identifier in base_ids if identifier not in selected_ids
        ]
        base_trace["structural_additions"] = sum(
            candidate["chunk_id"] not in set(base_ids) for candidate in selected
        )
        base_trace["a_retention_at_40"] = (
            len(set(base_ids).intersection(selected_ids)) / len(base_ids) if base_ids else None
        )
        check_deadline(request)
        return {"candidates": selected, "trace": base_trace}
