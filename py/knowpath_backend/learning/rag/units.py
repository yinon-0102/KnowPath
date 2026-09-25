"""Deterministic, leaf-backed semantic units for B3 retrieval."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .retrieval import content_hash
from .contracts import SourceSpan


def _token_count(row):
    quality = row.get("quality")
    value = quality.get("token_count") if isinstance(quality, dict) else None
    if type(value) is int and value >= 0:
        return value
    return len(row["source_text"].encode("utf-8"))


@dataclass(frozen=True)
class SemanticUnit:
    """A rerank/navigation record whose final evidence remains leaf-backed."""

    unit_id: str
    anchor_id: str
    retrieval_version_id: str
    material_version_id: str
    tree_version_id: str
    parent_id: str | None
    leaf_ids: tuple[str, ...]
    source_spans: tuple[dict, ...]
    source_text: str
    retrieval_text: str
    token_count: int
    source_map_hash: str
    ordinal: int


def _invalid():
    raise ValueError("STRUCTURE_UNAVAILABLE")


def build_unit(rows, *, tree_version_id):
    """Build one canonical unit from authorized leaf rows.

    Input order is intentionally ignored. Identity and source order come from
    immutable leaf ordinals, never from model output or chunk-id order.
    """
    rows = list(rows)
    if not rows or not isinstance(tree_version_id, str) or not tree_version_id:
        _invalid()
    try:
        if any(not isinstance(row, dict) or type(row.get("ordinal")) is not int
               or row["ordinal"] < 0 for row in rows):
            _invalid()
        members = sorted(rows, key=lambda row: (row["ordinal"], row["chunk_id"]))
        if len({row["ordinal"] for row in members}) != len(members):
            _invalid()
        identities = {
            (row["material_version_id"], row["retrieval_version_id"],
             row.get("parent_id"), row.get("tree_version_id"))
            for row in members
        }
        if len(identities) != 1 or next(iter(identities))[3] != tree_version_id:
            _invalid()
        if any(not isinstance(row["chunk_id"], str) or not row["chunk_id"]
               or not isinstance(row["source_text"], str) or not row["source_text"].strip()
               or not isinstance(row["retrieval_text"], str) or not row["retrieval_text"].strip()
               for row in members):
            _invalid()
        roots = [row for row in members if row.get("continuation_of") is None]
        if len(roots) != 1 or roots[0] is not members[0]:
            _invalid()
        anchor_id = roots[0]["chunk_id"]
        if not isinstance(anchor_id, str) or not anchor_id:
            _invalid()
        if any(row.get("continuation_of") != anchor_id for row in members[1:]):
            _invalid()
        leaf_ids = tuple(row["chunk_id"] for row in members)
        if len(set(leaf_ids)) != len(leaf_ids):
            _invalid()
        material_version_id, retrieval_version_id, parent_id, _ = next(iter(identities))
        if any(not isinstance(value, str) or not value for value in
               (material_version_id, retrieval_version_id)):
            _invalid()
        source_map, previous_start = [], {}
        for row in members:
            if not isinstance(row.get("source_spans"), (list, tuple)) or not row["source_spans"]:
                _invalid()
            leaf_spans = []
            for value in row["source_spans"]:
                span = value if isinstance(value, SourceSpan) else SourceSpan.model_validate(value)
                if span.material_version_id != material_version_id:
                    _invalid()
                position = (span.artifact_hash, span.page, span.block)
                if span.start < previous_start.get(position, 0):
                    _invalid()
                previous_start[position] = span.start
                # Preserve the original source-map representation and hashes.
                leaf_spans.append(dict(value) if isinstance(value, dict)
                                  else span.model_dump(mode="json"))
            source_map.append({"leaf_id": row["chunk_id"], "source_spans": leaf_spans})
        spans = tuple(span for item in source_map for span in item["source_spans"])
        source_map_hash = content_hash(json.dumps(source_map, ensure_ascii=False,
                                                   sort_keys=True, separators=(",", ":")))
        identity = ["b3-unit-v1", retrieval_version_id, material_version_id,
                    tree_version_id, leaf_ids]
        unit_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
        source_text = "\n".join(row["source_text"] for row in members)
        retrieval_text = "\n".join(row["retrieval_text"] for row in members)
        return SemanticUnit(unit_id=unit_id, anchor_id=anchor_id,
                            retrieval_version_id=retrieval_version_id,
                            material_version_id=material_version_id,
                            tree_version_id=tree_version_id, parent_id=parent_id,
                            leaf_ids=leaf_ids, source_spans=spans,
                            source_text=source_text, retrieval_text=retrieval_text,
                            token_count=sum(_token_count(row) for row in members),
                            source_map_hash=source_map_hash,
                            ordinal=members[0]["ordinal"])
    except (KeyError, TypeError, ValueError):
        _invalid()


def build_units(rows, *, tree_version_id=None):
    """Build each tree's units independently, then merge deterministically."""
    groups = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("chunk_id") in seen:
            _invalid()
        key = row.get("continuation_of") or row.get("chunk_id")
        if not isinstance(key, str) or not key:
            _invalid()
        seen.add(row["chunk_id"])
        groups.setdefault(key, []).append(row)
    units = [build_unit(group, tree_version_id=tree_version_id or group[0].get("tree_version_id"))
             for group in groups.values()]
    return tuple(sorted(units, key=lambda unit: (unit.retrieval_version_id, unit.tree_version_id,
                                                 unit.ordinal, unit.unit_id)))
