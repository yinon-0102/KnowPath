"""Record source consultation before exposing text during an active assessment."""
from __future__ import annotations

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.spaces.service import now

ACTIVE_ASSESSMENTS = {"generating", "ready", "in_progress"}


def source_key(ref):
    return ref.get("material_id"), ref.get("material_version_id"), ref.get("chunk_id")


def apply_source_assistance(questions, snapshot):
    consulted = {source_key(ref) for ref in snapshot.get("consulted_sources", [])}
    for question in questions:
        if (snapshot.get("all_topics_assisted") or question.get("topic_id") in snapshot.get("consulted_topic_ids", [])
                or any(source_key(ref) in consulted for ref in question.get("source_refs", []))):
            question["assisted"] = True


class SourceAccessService:
    def __init__(self, assessments):
        self.assessments = assessments
        self.repository = assessments.repository

    def consult_citation(self, citation, space_id, *, rag_repository=None):
        """Versioned citation access preserves the existing assistance audit."""
        from knowpath_backend.learning.rag.sources import CitationResolver
        spaces = self.assessments.spaces
        return CitationResolver(spaces.materials, spaces, source_access=self,
                                rag_repository=rag_repository).resolve(citation, space_id=space_id)

    def consult(self, material_id, version_id, chunk_ids, space_id=None):
        def bound(bindings):
            return any(b["material_id"] == material_id and b["material_version_id"] == version_id for b in bindings)

        with self.repository.transaction():
            # Lock only matching assessment aggregates, in the same order as
            # finalize (assessment -> space), never every row in the store.
            candidates = [a for a in self.repository.records("assessments", lock=False)
                          if a["status"] in ACTIVE_ASSESSMENTS and bound(a["snapshot"]["bindings"])]
            active = []
            for candidate in sorted(candidates, key=lambda a: a["id"]):
                try:
                    current = self.repository.get_record("assessments", candidate["id"])
                except DomainNotFound:
                    continue
                if current["status"] not in ACTIVE_ASSESSMENTS:
                    continue
                if current["status"] == "generating":
                    run = self.assessments.runs.get(current["run_id"])
                    if run["status"] in {"failed", "cancelled"}:
                        continue
                active.append(current)
            if active and space_id is None:
                raise DomainConflict("SPACE_CONTEXT_REQUIRED", "活动测验期间查阅原文必须提供 space_id")
            if space_id is not None:
                space = self.assessments.spaces.repository.get(space_id)
                if not bound(space["bindings"]) and not any(a["space_id"] == space_id for a in active):
                    raise DomainConflict("SOURCE_OUT_OF_SCOPE", "来源不属于该空间绑定或活动测验的资料版本")
            if not active:
                return
            consulted = [{"material_id": material_id, "material_version_id": version_id,
                          "chunk_id": chunk_id, "accessed_at": now()} for chunk_id in chunk_ids]
            for assessment in active:
                snapshot = assessment["snapshot"]
                previous = snapshot.setdefault("consulted_sources", [])
                seen = {source_key(ref) for ref in previous}
                previous.extend(ref for ref in consulted if source_key(ref) not in seen)
                apply_source_assistance(assessment["questions"], snapshot)
                self.repository.put_record("assessments", assessment)
