"""Record source consultation before exposing text during an active assessment."""
from __future__ import annotations
from uuid import uuid4

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.spaces.service import now

ACTIVE_ASSESSMENTS = {"generating", "ready", "in_progress"}


class AssistanceDeliveryChanged(Exception):
    """Retry publication when an assessment was created before space locking."""


def source_key(ref):
    return ref.get("material_id"), ref.get("material_version_id"), ref.get("chunk_id")


def original_source_refs(sources):
    """Project verified retrieval spans onto assessment/source-read identities."""
    refs = {}
    for source in sources:
        blocks = ([span["block"] for span in source["source_spans"]]
                  if source.get("citation_schema_version") == 2 else [source["chunk_id"]])
        for block in blocks:
            ref = {"material_id": source["material_id"],
                   "material_version_id": source["material_version_id"], "chunk_id": block}
            refs[source_key(ref)] = ref
    return list(refs.values())


def assessment_source_keys(assessment):
    rows = assessment["questions"] or assessment["snapshot"].get("topics", [])
    return {source_key(ref) for row in rows for ref in row.get("source_refs", [])}


def matches_delivery(assessment, keys, bound_versions=None):
    if assessment["status"] not in ACTIVE_ASSESSMENTS:
        return False
    if bound_versions is not None:
        return any((b["material_id"], b["material_version_id"]) in bound_versions
                   for b in assessment["snapshot"]["bindings"])
    return bool(assessment_source_keys(assessment) & keys)


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

    def prepare_delivery(self, sources, space_id, *, include_bound_assessments=False):
        """Discover related aggregates outside the publication transaction.

        The store represents one learner. Every space sharing these original
        sources participates, including assessments whose questions are pending.
        """
        refs = original_source_refs(sources)
        keys = {source_key(ref) for ref in refs}
        bindings = {(ref["material_id"], ref["material_version_id"]) for ref in refs}
        bound_versions = bindings if include_bound_assessments else None
        candidates = [a for a in self.repository.records("assessments", lock=False)
                      if matches_delivery(a, keys, bound_versions)]
        related = {space["id"] for space in self.assessments.spaces.repository.list()
                   if any((b["material_id"], b["material_version_id"]) in bindings for b in space["bindings"])}
        return {"sources": refs, "keys": keys, "bound_versions": bound_versions,
                "assessment_ids": sorted(a["id"] for a in candidates),
                "space_ids": sorted(related | ({space_id} if space_id is not None else set())
                                    | {a["space_id"] for a in candidates})}

    def lock_delivery(self, plan, space_id):
        """Called in the response transaction, before space/message/run locks."""
        active = []
        for identifier in plan["assessment_ids"]:
            try:
                assessment = self.repository.get_record("assessments", identifier)
            except DomainNotFound:
                continue
            if matches_delivery(assessment, plan["keys"], plan["bound_versions"]):
                active.append(assessment)
        spaces = {}
        for identifier in plan["space_ids"]:
            try:
                spaces[identifier] = self.assessments.spaces.repository.get(identifier)
            except DomainNotFound:
                if identifier == space_id:
                    raise
        # Assessment creation locks its source versions before inserting the
        # aggregate. This also fences assessments in a brand-new related space
        # absent from the discovery plan, without taking every space lock.
        for identifier in sorted({ref["material_version_id"] for ref in plan["sources"]}):
            version = self.assessments.spaces.materials.get_version(identifier)
            if version is None or any(ref["material_id"] != version.material_id
                                      for ref in plan["sources"] if ref["material_version_id"] == identifier):
                raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "回答来源已删除或不可用")
        # This is deliberately the first non-locking read in this transaction:
        # MySQL REPEATABLE READ must see assessments committed before the space
        # locks. Creation uses the same space lock. A newly discovered aggregate
        # requires rollback/retry, never an assessment lock taken after a space.
        current = {a["id"] for a in self.repository.records("assessments", lock=False)
                   if matches_delivery(a, plan["keys"], plan["bound_versions"])}
        if current - set(plan["assessment_ids"]):
            raise AssistanceDeliveryChanged()
        return active, spaces.get(space_id)

    def record_delivery(self, assessments, plan, *, kind, delivery_id, space_id, message_id=None):
        """Commit assistance with response events; a failed delivery leaves none."""
        timestamp = now()
        for assessment in assessments:
            if assessment["status"] == "generating":
                run = self.assessments.runs.get(assessment["run_id"])
                if run["status"] not in {"queued", "running"}:
                    continue
            snapshot = assessment["snapshot"]
            events = snapshot.setdefault("assistance_events", [])
            if any(event.get("kind") == kind and event.get("delivery_id") == delivery_id for event in events):
                continue
            keys = assessment_source_keys(assessment)
            refs = [dict(ref, accessed_at=timestamp) for ref in plan["sources"] if source_key(ref) in keys]
            if not refs:
                continue
            previous = snapshot.setdefault("consulted_sources", [])
            seen = {source_key(ref) for ref in previous}
            previous.extend(ref for ref in refs if source_key(ref) not in seen)
            topics = [topic["id"] for topic in snapshot.get("topics", [])
                      if any(source_key(ref) in plan["keys"] for ref in topic.get("source_refs", []))]
            event = {"kind": kind, "delivery_id": delivery_id, "space_id": space_id,
                     "delivered_at": timestamp, "source_refs": refs, "topic_ids": topics}
            if message_id is not None or kind == "message":
                event["message_id"] = message_id or delivery_id
            events.append(event)
            apply_source_assistance(assessment["questions"], snapshot)
            self.repository.put_record("assessments", assessment)

    def consult_citation(self, citation, space_id, *, rag_repository=None):
        """Versioned citation access preserves the existing assistance audit."""
        from knowpath_backend.learning.rag.sources import CitationResolver
        spaces = self.assessments.spaces
        return CitationResolver(spaces.materials, spaces, source_access=self,
                                rag_repository=rag_repository).resolve(citation, space_id=space_id)

    def consult(self, material_id, version_id, chunk_ids, space_id=None):
        def bound(bindings):
            return any(b["material_id"] == material_id and b["material_version_id"] == version_id for b in bindings)

        refs = [{"material_id": material_id, "material_version_id": version_id,
                 "chunk_id": chunk_id} for chunk_id in chunk_ids]
        delivery_id = str(uuid4())
        for _ in range(3):
            # Keep the existing whole-version context requirement even when
            # this particular chunk does not overlap the assessment questions.
            plan = self.prepare_delivery(refs, space_id, include_bound_assessments=True)
            def deliver():
                active, space = self.lock_delivery(plan, space_id)
                active = [a for a in active if a["status"] != "generating"
                          or self.assessments.runs.get(a["run_id"])["status"] in {"queued", "running"}]
                if active and space_id is None:
                    raise DomainConflict("SPACE_CONTEXT_REQUIRED", "活动测验期间查阅原文必须提供 space_id")
                if space is not None and not bound(space["bindings"]) and not any(a["space_id"] == space_id for a in active):
                    raise DomainConflict("SOURCE_OUT_OF_SCOPE", "来源不属于该空间绑定或活动测验的资料版本")
                self.record_delivery(active, plan, kind="source_read", delivery_id=delivery_id, space_id=space_id)
            try:
                return self.assessments.commands._execute("source.consult", space_id, {}, None, deliver)
            except AssistanceDeliveryChanged:
                continue
        raise DomainConflict("STALE_LEARNING_CONTEXT", "学习范围已变化，请重新读取来源")
