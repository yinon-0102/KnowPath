"""Exact original-coordinate authorization, independent of retrieval versions."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping

from .contracts import ScopeBinding, ScopeSnapshot, SourceSpan
from ..errors import DomainConflict


def coordinate_key(span: SourceSpan) -> tuple:
    return span.material_version_id, span.artifact_hash, span.page, span.block


def merge_spans(spans: Iterable[SourceSpan]) -> tuple[SourceSpan, ...]:
    groups = defaultdict(list)
    for span in spans:
        groups[coordinate_key(span)].append(span)
    merged = []
    for key in sorted(groups, key=lambda key: json.dumps(key, ensure_ascii=False)):
        pending = None
        for span in sorted(groups[key], key=lambda span: (span.start, span.end)):
            if pending is not None and span.start <= pending.end:
                pending = pending.model_copy(update={"end": max(pending.end, span.end)})
            else:
                if pending is not None:
                    merged.append(pending)
                pending = span
        if pending is not None:
            merged.append(pending)
    return tuple(merged)


def covers(span: SourceSpan, allowed: Iterable[SourceSpan]) -> bool:
    """Require union coverage of every character; partial overlap is not access."""
    cursor = span.start
    for item in sorted((s for s in allowed if coordinate_key(s) == coordinate_key(span)),
                       key=lambda s: (s.start, s.end)):
        if item.start > cursor:
            return False
        cursor = max(cursor, item.end)
        if cursor >= span.end:
            return True
    return False


def freeze_scope(*, space_id: str, scope_version: int, bindings: Iterable[ScopeBinding],
                 allowed_spans: Iterable[SourceSpan]) -> ScopeSnapshot:
    data = dict(space_id=space_id, scope_version=scope_version,
                bindings=tuple(sorted(bindings, key=lambda b: (b.material_id, b.material_version_id))),
                allowed_spans=merge_spans(allowed_spans))
    validated = ScopeSnapshot(scope_snapshot_id="pending", **data)
    canonical = json.dumps(validated.model_dump(exclude={"scope_snapshot_id"}),
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return validated.model_copy(update={"scope_snapshot_id": hashlib.sha256(canonical.encode()).hexdigest()})


def allowed_chunk_ids(scope: ScopeSnapshot,
                      chunks: Mapping[str, Iterable[SourceSpan]]) -> tuple[str, ...]:
    selected = []
    for identifier, spans in chunks.items():
        spans = tuple(spans)
        if spans and all(covers(span, scope.allowed_spans) for span in spans):
            selected.append(identifier)
    return tuple(selected)


def legacy_span(version_id, chunk) -> SourceSpan:
    """Pin existing extraction coordinates without pretending to reparse the file.

    Each old chunk is a separate immutable artifact/block. A future parser must
    prove its mapping into these coordinates; equal offsets alone never map it.
    """
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    if digest != chunk.content_hash or not chunk.text:
        raise DomainConflict("SOURCE_MAPPING_INVALID", "旧来源正文与哈希不一致")
    return SourceSpan(material_version_id=version_id, artifact_hash=digest,
                      start=0, end=len(chunk.text), page=chunk.page, block=chunk.id)


def learning_scope(space, topics, materials) -> ScopeSnapshot:
    bindings = tuple(ScopeBinding(**{key: b[key] for key in
        ("material_id", "material_version_id", "graph_version")}) for b in space["bindings"])
    versions = {}
    by_version = {b.material_version_id: b for b in bindings}
    for binding in bindings:
        material = materials.get_material(binding.material_id)
        version = materials.get_version(binding.material_version_id)
        if (material is None or material.status == "archived" or version is None
                or version.material_id != binding.material_id or version.status != "ready"):
            raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "绑定原文已删除或不可用")
        versions[version.id] = {chunk.id: chunk for chunk in version.chunks}
    aliases = {t["id"]: t.get("canonical_topic_id", t["id"]) for t in topics}
    selected = {aliases.get(t, t) for t in space["topic_ids"]}
    excluded = {aliases.get(t, t) for t in space["excluded_topic_ids"]}
    permitted, denied = {}, set()
    for topic in topics:
        canonical = aliases[topic["id"]]
        if selected and canonical not in selected and canonical not in excluded:
            continue
        for ref in topic["source_refs"]:
            binding = by_version.get(ref["material_version_id"])
            if (binding is None or binding.material_id != ref["material_id"]
                    or topic["graph_version"] != binding.graph_version):
                raise DomainConflict("SOURCE_MAPPING_INVALID", "学习图谱来源绑定不匹配")
            key = ref["material_version_id"], ref["chunk_id"]
            chunk = versions[key[0]].get(key[1])
            if chunk is None:
                raise DomainConflict("SOURCE_MAPPING_INVALID", "学习图谱来源无法定位")
            if canonical in excluded:
                denied.add(key)
            else:
                permitted[key] = legacy_span(key[0], chunk)
    return freeze_scope(space_id=space["id"], scope_version=space["scope_version"],
                        bindings=bindings, allowed_spans=[s for key, s in permitted.items() if key not in denied])
