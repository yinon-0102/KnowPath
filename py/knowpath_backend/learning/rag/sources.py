"""Resolve new citations through verified original spans and retain legacy audit."""
from __future__ import annotations

from .contracts import Citation, SourceSpan
from .scope import covers, legacy_span
from ..errors import DomainConflict


class CitationResolver:
    def __init__(self, materials, spaces, *, source_access, rag_repository=None):
        self.materials, self.spaces = materials, spaces
        self.source_access, self.rag = source_access, rag_repository

    def resolve(self, citation: dict, *, space_id: str) -> dict:
        scope = self.spaces.rag_scope_snapshot(space_id)
        schema = citation.get("citation_schema_version", 1)
        if type(schema) is not int or schema not in (1, 2):
            raise ValueError("unsupported citation schema")
        material_id, version_id = citation["material_id"], citation["material_version_id"]
        version = self.materials.get_version(version_id)
        material = self.materials.get_material(material_id)
        if (material is None or material.status == "archived" or version is None
                or version.material_id != material_id):
            raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "引用原文已删除或不可用")
        chunks = {chunk.id: chunk for chunk in version.chunks}
        if schema == 1:
            chunk = chunks.get(citation["chunk_id"])
            if chunk is None:
                raise DomainConflict("SOURCE_MAPPING_INVALID", "旧引用无法定位")
            spans = (legacy_span(version_id, chunk),)
            if not covers(spans[0], scope.allowed_spans):
                raise DomainConflict("SOURCE_OUT_OF_SCOPE", "引用不在当前允许范围")
            result = dict(citation, text=chunk.text, page=chunk.page, line_start=chunk.line_start,
                          line_end=chunk.line_end, section_path=list(chunk.section_path))
        else:
            parsed = Citation.model_validate(citation)
            stored = self.rag.get_chunk(parsed.retrieval_version_id, parsed.chunk_id) if self.rag else None
            if stored is None or stored["material_version_id"] != version_id:
                raise DomainConflict("SOURCE_MAPPING_INVALID", "新引用索引版本不匹配")
            whole = tuple(SourceSpan.model_validate(s) for s in stored["source_spans"])
            # Even a permitted excerpt cannot rescue a partially excluded chunk.
            if not whole or any(not covers(s, scope.allowed_spans) for s in whole):
                raise DomainConflict("SOURCE_OUT_OF_SCOPE", "新分块包含当前范围外内容")
            spans = parsed.source_spans
            if any(not covers(s, whole) for s in spans):
                raise DomainConflict("SOURCE_MAPPING_INVALID", "引用跨度不属于索引原文")
            parts = []
            for s in spans:
                chunk = chunks.get(s.block)
                if chunk is None or not covers(s, [legacy_span(version_id, chunk)]):
                    raise DomainConflict("SOURCE_MAPPING_INVALID", "原文坐标尚未可靠映射")
                parts.append(chunk.text[s.start:s.end])
            result = dict(parsed.model_dump(), text="\n".join(parts))
        # Consult the OLD source identities so current/pending assessments cannot
        # mistake a new retrieval ID for an independent, unassisted answer.
        self.source_access.consult(material_id, version_id,
                                   list(dict.fromkeys(s.block for s in spans)), space_id)
        if self.spaces.rag_scope_snapshot(space_id).scope_snapshot_id != scope.scope_snapshot_id:
            raise DomainConflict("SCOPE_CHANGED", "读取期间学习范围已变化")
        return result
