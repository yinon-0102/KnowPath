"""Learning runtime wiring durable spaces/assessments and provisional plan/session workflows."""

from __future__ import annotations

import copy
from typing import Any

from knowpath_backend.learning.materials.service import InMemoryMaterialRepository, MaterialRepository, MaterialService
from .errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.workers.runs import RunService
from knowpath_backend.learning.spaces.service import SpaceService, topics_for_version
from knowpath_backend.learning.persistence.space_repository import InMemorySpaceRepository, SqlAlchemySpaceRepository
from knowpath_backend.learning.persistence.learning_repository import InMemoryLearningRepository, SqlAlchemyLearningRepository
from knowpath_backend.learning.assessments.service import AssessmentService
from knowpath_backend.learning.persistence.run_repository import SqlAlchemyRunRepository
from knowpath_backend.learning.plans.service import PlanSessionService
from knowpath_backend.learning.spaces.exports import ExportService
from knowpath_backend.learning.spaces.deletion import SpaceDeletionService
from knowpath_backend.learning.conversations.service import MessageService
from knowpath_backend.learning.knowledge.reconciliation import GraphReconciliationService
from knowpath_backend.learning.knowledge.corrections import CorrectionService
from knowpath_backend.learning.knowledge.updates import KnowledgeUpdateService
from knowpath_backend.learning.knowledge.queries import GraphQueryService
from knowpath_backend.learning.persistence.graph_repository import InMemoryGraphRepository, SqlAlchemyGraphRepository



class LearningState:
    def __init__(self, material_repository: MaterialRepository | None = None, *,
                 run_service: RunService | None = None, space_service: SpaceService | None = None, question_generator=None, answer_generator=None, source_retriever=None, context_settings=None, rag_pipeline=None) -> None:
        self.material_repository = material_repository or InMemoryMaterialRepository()
        self.material_service = MaterialService(self.material_repository)
        uow = getattr(self.material_repository, "unit_of_work", None)
        if run_service is None:
            run_service = RunService(SqlAlchemyRunRepository(uow.engine, unit_of_work=uow) if uow
                                     else self.material_repository.run_repository)
        self.run_service = run_service
        if space_service is None:
            uow = getattr(self.material_repository, "unit_of_work", None)
            repository = SqlAlchemySpaceRepository(uow) if uow else InMemorySpaceRepository(self.material_repository)
            space_service = SpaceService(repository, self.material_repository)
        self.space_service = space_service
        learning_repository = SqlAlchemyLearningRepository(uow) if uow else InMemoryLearningRepository(self.material_repository, self.run_service)
        graph_repository = SqlAlchemyGraphRepository(uow) if uow else InMemoryGraphRepository(self.material_repository, self.run_service)
        self.graph_service = GraphReconciliationService(graph_repository, self.material_repository, self.run_service)
        from knowpath_backend.learning.materials.deletion import MaterialDeletionService
        self.material_deletion_service = MaterialDeletionService(graph_repository, space_service, self.graph_service, self.run_service)
        self.graph_queries = GraphQueryService(self.graph_service)
        self.space_service.graphs = self.graph_service
        self.correction_service = CorrectionService(self.graph_service, self.space_service)
        self.knowledge_update_service = KnowledgeUpdateService(learning_repository, space_service, self.graph_service, self.run_service)
        self.assessment_service = AssessmentService(learning_repository, space_service, self.run_service, question_generator)
        if rag_pipeline is None:
            from .rag.runtime import configured_pipeline
            rag_pipeline = configured_pipeline(self.material_repository, space_service, context_settings)
        self.message_service = MessageService(learning_repository, space_service, self.assessment_service, self.run_service, answer_generator, source_retriever, context_settings, rag_pipeline)
        self.spaces: dict[str, dict[str, Any]] = {}
        self.topics: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.changes: list[dict[str, Any]] = []
        self.plan_sessions = PlanSessionService(learning_repository, space_service, self.run_service, self.assessment_service, self.plans, self.sessions)
        self.export_service = ExportService(learning_repository, space_service, self.assessment_service, self.plan_sessions, self.run_service)

    def delete_material(self, material_id, payload):
        return self.material_deletion_service.delete(material_id, payload)

    def run(self, kind: str, result_ref: dict[str, str] | None = None, *, status: str = "succeeded") -> dict[str, Any]:
        return self.run_service.create(kind, result_ref, status=status)

    def events_for(self, run_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        return self.run_service.events_for(run_id, after_id=after_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.run_service.get(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self.run_service.request_cancel(run_id)

    def _hydrate_space(self, metadata):
        space = self.spaces.setdefault(metadata["id"], {"state_version": 0, "state": {}, "active_session_id": None})
        space.update(copy.deepcopy(metadata))
        space["active_session_id"] = self.plan_sessions.active_session_id(metadata["id"])
        return space

    def create_space(self, payload: dict[str, Any], *, idempotency_key: str | None = None, require_published=False) -> dict[str, Any]:
        # Committed responses replay until deletion replaces them with tombstones.
        # Topics and runtime state are hydrated on demand from pinned bindings.
        return self.space_service.create(payload, idempotency_key, require_published=require_published)

    def list_spaces(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self._hydrate_space(space)) for space in self.space_service.list()]

    def get_space(self, space_id: str) -> dict[str, Any]:
        return self._hydrate_space(self.space_service.get(space_id))

    def update_space(self, space_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.space_service.update(space_id, payload, idempotency_key)

    def set_scope(self, space_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        result = self.space_service.set_scope(space_id, payload, idempotency_key)
        try:
            current = self.get_space(space_id)
        except DomainNotFound:
            # A committed idempotent response can be replayed after the resource
            # was deleted; no runtime plan cache needs invalidation then.
            return result
        for plan in self.plans.values():
            if plan["space_id"] == space_id and plan.get("scope_version", 0) != current["scope_version"]:
                plan["status"] = "needs_replan"
        return result

    def get_profile(self, space_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.get_space(space_id)["profile"])

    def update_profile(self, space_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.space_service.update_profile(space_id, payload, idempotency_key)

    def _index_material_topics(self, material_id: str, version: Any) -> None:
        for topic in topics_for_version(material_id, version):
            self.topics[topic["id"]] = topic

    def topics_for_space(self, space_id: str) -> list[dict[str, Any]]:
        space = self.get_space(space_id)
        topics = self.space_service.bound_topics(space)
        for topic in topics:
            self.topics[topic["id"]] = topic
        selected = set(space["topic_ids"])
        excluded = set(space["excluded_topic_ids"])
        return [copy.deepcopy(topic) for topic in topics
                if (not selected or topic["id"] in selected) and topic["id"] not in excluded]

    def get_topic_graph(self, topic_id: str, *, depth=1, include_sources=True) -> dict[str, Any]:
        return self.graph_queries.topic_graph(topic_id, depth=depth, include_sources=include_sources)

    def create_assessment(self, space_id, payload, *, idempotency_key=None, dispatch=None, durable=False):
        return self.assessment_service.create(space_id, payload, idempotency_key, dispatch=dispatch, durable=durable)

    def get_assessment(self, assessment_id):
        return self.assessment_service.get(assessment_id)

    def record_attempt(self, assessment_id, payload, *, idempotency_key=None):
        return self.assessment_service.record(assessment_id, payload, idempotency_key)

    def finalize_assessment(self, assessment_id, payload):
        return self.assessment_service.finalize(assessment_id, payload)

    def assessment_result(self, assessment_id):
        return self.assessment_service.result(assessment_id)

    def get_state(self, space_id, **filters):
        return self.assessment_service.state(space_id, **filters)

    def evidence_for(self, space_id, **filters):
        return self.assessment_service.evidence(space_id, **filters)

    def create_plan(self, space_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.plan_sessions.create_plan(space_id, payload, idempotency_key)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self.plan_sessions.get_plan(plan_id)

    def update_task(self, plan_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.plan_sessions.update_task(plan_id, task_id, payload)

    def start_session(self, plan_id: str, task_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.plan_sessions.start_session(plan_id, task_id, idempotency_key)

    def add_session_event(self, session_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.plan_sessions.add_event(session_id, payload, idempotency_key=idempotency_key)

    def finish_session(self, session_id: str) -> dict[str, Any]:
        return self.plan_sessions.finish_session(session_id)

    def send_message(self, space_id: str, payload: dict[str, Any], *, idempotency_key=None, dispatch=None, durable=False) -> dict[str, Any]:
        return self.message_service.send(space_id, payload, idempotency_key, dispatch=dispatch, durable=durable)

    def create_correction(self, space_id, payload, *, idempotency_key=None):
        return self.correction_service.create(space_id, payload, idempotency_key)

    def confirm_correction(self, space_id, correction_id, payload, *, idempotency_key=None):
        return self.correction_service.confirm(space_id, correction_id, payload, idempotency_key)

    def knowledge_updates(self, space_id: str) -> dict[str, Any]:
        return self.knowledge_update_service.preview(space_id)

    def apply_knowledge_updates(self, space_id, payload, *, idempotency_key=None):
        return self.knowledge_update_service.apply(space_id, payload, idempotency_key)

    def grade_review(self, assessment_id, payload, idempotency_key=None):
        return self.assessment_service.review_grade(assessment_id, payload, idempotency_key)

    def reset_state(self, space_id, topic_ids, reason, *, expected_state_version, idempotency_key=None):
        return self.assessment_service.reset(space_id, {"topic_ids": topic_ids, "reason": reason,
            "expected_state_version": expected_state_version}, idempotency_key)

    def create_export(self, space_id, payload=None, idempotency_key=None):
        return self.export_service.create(space_id, payload, idempotency_key)

    def export_payload(self, export_id):
        return self.export_service.payload(export_id)

    def export_archive(self, export_id):
        return self.export_service.archive(export_id)

    def delete_space(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = SpaceDeletionService(self.assessment_service.repository, self.space_service, self.run_service).delete(space_id, payload)
        self.spaces.pop(space_id, None)
        for cache in (self.plans, self.sessions):
            for identifier, row in list(cache.items()):
                if row.get("space_id") == space_id:
                    del cache[identifier]
        self.changes[:] = [row for row in self.changes if row.get("space_id") != space_id]
        return result

    def changes_for(self, space_id: str) -> list[dict[str, Any]]:
        from knowpath_backend.learning.spaces.timeline import changes_for
        return changes_for(self, space_id)
