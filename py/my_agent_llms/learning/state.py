"""Learning runtime with durable space metadata and in-memory learning workflows.

SpaceService owns space metadata; these dictionaries retain transient assessment,
plan, evidence and session state until their repositories are implemented.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .materials import InMemoryMaterialRepository, MaterialRepository, MaterialService
from .errors import DomainConflict, DomainNotFound
from .runs import RunService
from .spaces import SpaceService, topics_for_version
from .space_repository import InMemorySpaceRepository, SqlAlchemySpaceRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class LearningState:
    def __init__(self, material_repository: MaterialRepository | None = None, *,
                 run_service: RunService | None = None, space_service: SpaceService | None = None) -> None:
        self.material_repository = material_repository or InMemoryMaterialRepository()
        self.material_service = MaterialService(self.material_repository)
        self.run_service = run_service if run_service is not None else RunService()
        if space_service is None:
            uow = getattr(self.material_repository, "unit_of_work", None)
            repository = SqlAlchemySpaceRepository(uow) if uow else InMemorySpaceRepository(self.material_repository)
            space_service = SpaceService(repository, self.material_repository)
        self.space_service = space_service
        self.spaces: dict[str, dict[str, Any]] = {}
        self.topics: dict[str, dict[str, Any]] = {}
        self.assessments: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.corrections: dict[str, dict[str, Any]] = {}
        self.exports: dict[str, dict[str, Any]] = {}
        self.changes: list[dict[str, Any]] = []

    def run(self, kind: str, result_ref: dict[str, str] | None = None, *, status: str = "succeeded") -> dict[str, Any]:
        return self.run_service.create(kind, result_ref, status=status)

    def events_for(self, run_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        return self.run_service.events_for(run_id, after_id=after_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.run_service.get(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self.run_service.request_cancel(run_id)

    def _hydrate_space(self, metadata):
        # Only metadata is durable in this slice. Assessment state and active
        # sessions remain owned by this runtime until their repositories land.
        space = self.spaces.setdefault(metadata["id"], {"state_version": 0, "state": {}, "active_session_id": None})
        space.update(copy.deepcopy({key: value for key, value in metadata.items()
                                    if key not in {"state", "state_version", "active_session_id"}}))
        return space

    def create_space(self, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        # The committed response is authoritative, including retries after deletion.
        # Topics and runtime state are hydrated on demand from pinned bindings.
        return self.space_service.create(payload, idempotency_key)

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

    def get_topic_graph(self, topic_id: str) -> dict[str, Any]:
        topic = self.topics.get(topic_id)
        if topic is None:
            for space in self.space_service.list():
                for restored in self.space_service.bound_topics(space):
                    self.topics[restored["id"]] = restored
            topic = self.topics.get(topic_id)
        if topic is None:
            raise DomainNotFound("topic", topic_id)
        return {"nodes": [copy.deepcopy(topic)], "edges": [], "graph_version": topic["graph_version"]}

    def create_assessment(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        space = self.get_space(space_id)
        topics = self.topics_for_space(space_id)
        count = max(1, min(int(payload.get("question_count", 5)), 10))
        questions = []
        for index in range(count):
            topic = topics[index % len(topics)] if topics else {"id": "topic_general", "name": "基础知识", "source_refs": []}
            questions.append(
                {
                    "id": _id("question"),
                    "kind": "single_choice",
                    "topic_id": topic["id"],
                    "topic_revision_id": f"topicrev_{topic['id']}",
                    "stem": f"关于 {topic['name']}，哪项描述最符合资料？",
                    "options": [{"id": "A", "text": "正确描述"}, {"id": "B", "text": "无关描述"}],
                    "source_refs": topic.get("source_refs", []),
                    "difficulty": "easy",
                    "answer_key": "A",
                    "rubric_version": "objective-v1",
                }
            )
        assessment = {
            "id": _id("assessment"),
            "space_id": space_id,
            "kind": payload.get("kind", "diagnostic"),
            "status": "open",
            "topic_ids": [topic["id"] for topic in topics],
            "questions": questions,
            "attempts": [],
            "finalized": False,
            "result": None,
            "created_at": _now(),
        }
        self.assessments[assessment["id"]] = assessment
        run = self.run("assessment_generation", {"type": "assessment", "id": assessment["id"]})
        assessment["run_id"] = run["id"]
        return self._public_assessment(assessment)

    def _public_assessment(self, assessment: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(assessment)
        for question in result.get("questions", []):
            question.pop("answer_key", None)
        result.pop("attempts", None)
        return result

    def get_assessment(self, assessment_id: str) -> dict[str, Any]:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise DomainNotFound("assessment", assessment_id)
        return self._public_assessment(assessment)

    def record_attempt(self, assessment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise DomainNotFound("assessment", assessment_id)
        if assessment["finalized"]:
            raise DomainConflict("ASSESSMENT_FINALIZED", "测验已经封存")
        answers = payload.get("answers") or []
        for item in answers:
            existing = next((a for a in assessment["attempts"] if a["question_id"] == item.get("question_id")), None)
            row = {"question_id": item.get("question_id"), "answer": item.get("answer"), "revision": int(item.get("expected_answer_revision", 0))}
            if existing:
                existing.update(row)
            else:
                assessment["attempts"].append(row)
        return {
            "attempt_id": _id("attempt"),
            "status": "recorded",
            "accepted_count": len(answers),
            "next_question_id": next((q["id"] for q in assessment["questions"] if q["id"] not in {a["question_id"] for a in assessment["attempts"]}), None),
        }

    def finalize_assessment(self, assessment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise DomainNotFound("assessment", assessment_id)
        if assessment["finalized"]:
            return {"run_id": assessment["finalize_run_id"], "assessment_id": assessment_id, "status": "processing"}
        answered = {item["question_id"]: item for item in assessment["attempts"]}
        if not payload.get("allow_unanswered", False) and len(answered) < len(assessment["questions"]):
            raise DomainConflict("ASSESSMENT_INCOMPLETE", "仍有未回答题目")
        question_results = []
        topic_scores: dict[str, list[float]] = {}
        for question in assessment["questions"]:
            answer = answered.get(question["id"])
            if answer is None:
                question_results.append({"question_id": question["id"], "verdict": "unverified", "score": None, "feedback": "未作答"})
                continue
            score = 1.0 if str(answer.get("answer", "")).strip().upper() == question["answer_key"] else 0.0
            verdict = "correct" if score else "incorrect"
            question_results.append({"question_id": question["id"], "verdict": verdict, "score": score, "feedback": "已按客观答案判分"})
            topic_scores.setdefault(question["topic_id"], []).append(score)
        space = self.get_space(assessment["space_id"])
        topic_results = []
        for topic_id, scores in topic_scores.items():
            score = sum(scores) / len(scores)
            state = space["state"].setdefault(topic_id, {"topic_id": topic_id, "evidence_ids": [], "error_tags": []})
            state.update({"mastery_score": score, "score_validity": "current", "status": "mastered" if score >= 0.8 else "learning", "last_assessed_at": _now(), "state_version": space["state_version"] + 1, "policy_version": "mastery-v1", "evidence_count": len(scores)})
            evidence_id = _id("evidence")
            evidence = {"id": evidence_id, "space_id": space["id"], "topic_id": topic_id, "assessment_id": assessment_id, "kind": "objective_answer", "result": "correct" if score >= 0.5 else "incorrect", "score": score, "created_at": _now()}
            self.evidence[evidence_id] = evidence
            state["evidence_ids"].append(evidence_id)
            topic_results.append({"topic_id": topic_id, "score": score, "verified_count": len(scores), "unverified_count": 0, "error_tags": []})
        space["state_version"] += 1
        result = {"assessment_id": assessment_id, "graded_at": _now(), "topic_results": topic_results, "question_results": question_results, "state_version": space["state_version"], "plan_replan_run_id": None}
        assessment["finalized"] = True
        assessment["status"] = "finalized"
        assessment["result"] = result
        run = self.run("assessment_finalize", {"type": "assessment", "id": assessment_id})
        assessment["finalize_run_id"] = run["id"]
        return {"run_id": run["id"], "assessment_id": assessment_id, "status": "processing"}

    def assessment_result(self, assessment_id: str) -> dict[str, Any]:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise DomainNotFound("assessment", assessment_id)
        if assessment["result"] is None:
            raise DomainConflict("ASSESSMENT_NOT_READY", "测验尚未完成评分")
        return copy.deepcopy(assessment["result"])

    def get_state(self, space_id: str) -> dict[str, Any]:
        space = self.get_space(space_id)
        return {"space_id": space_id, "state_version": space["state_version"], "items": copy.deepcopy(list(space["state"].values()))}

    def evidence_for(self, space_id: str, **_: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in self.evidence.values() if item["space_id"] == space_id]

    def create_plan(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        space = self.get_space(space_id)
        count = max(3, min(int(payload.get("session_count", 5)), 5))
        minutes = max(10, min(int(payload.get("minutes_per_session", 30)), 120))
        topic_ids = space["topic_ids"] or [topic["id"] for topic in self.topics_for_space(space_id)]
        tasks = []
        for index in range(count):
            topic_id = topic_ids[index % len(topic_ids)]
            tasks.append({"id": _id("task"), "topic_ids": [topic_id], "kind": "targeted_practice", "status": "pending", "estimated_minutes": minutes, "reason": "按当前学习范围生成"})
        plan_id = _id("plan")
        plan = {"plan_id": plan_id, "id": plan_id, "space_id": space_id, "version": 1, "scope_version": space["scope_version"], "tasks": tasks, "status": "ready", "created_at": _now()}
        self.plans[plan_id] = plan
        run = self.run("plan_build", {"type": "plan", "id": plan_id})
        plan["run_id"] = run["id"]
        return copy.deepcopy(plan)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DomainNotFound("plan", plan_id)
        try:
            current_scope_version = self.get_space(plan["space_id"])["scope_version"]
            if plan.get("scope_version", 0) != current_scope_version:
                plan["status"] = "needs_replan"
        except DomainNotFound:
            pass
        return copy.deepcopy(plan)

    def update_task(self, plan_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DomainNotFound("plan", plan_id)
        if payload.get("expected_plan_version") not in (None, plan["version"]):
            raise DomainConflict("VERSION_CONFLICT", "计划版本已变化")
        task = next((task for task in plan["tasks"] if task["id"] == task_id), None)
        if task is None:
            raise DomainNotFound("task", task_id)
        status = payload.get("status")
        if status not in {"completed", "skipped", "deferred"}:
            raise DomainConflict("INVALID_TASK_STATUS", "任务状态不合法")
        task["status"] = status
        if "note" in payload:
            task["note"] = payload["note"]
        if "defer_until" in payload:
            task["defer_until"] = payload["defer_until"]
        plan["version"] += 1
        return copy.deepcopy(task)

    def start_session(self, plan_id: str, task_id: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DomainNotFound("plan", plan_id)
        space = self.get_space(plan["space_id"])
        if space.get("active_session_id"):
            raise DomainConflict("SESSION_ACTIVE", "学习空间已有活动会话")
        session = {"id": _id("session"), "plan_id": plan_id, "space_id": space["id"], "task_id": task_id, "status": "active", "started_at": _now(), "events": []}
        self.sessions[session["id"]] = session
        space["active_session_id"] = session["id"]
        return copy.deepcopy(session)

    def add_session_event(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise DomainNotFound("session", session_id)
        event = {"id": _id("event"), "type": payload.get("type"), "topic_id": payload.get("topic_id"), "question_id": payload.get("question_id"), "received_at": _now()}
        session["events"].append(event)
        return copy.deepcopy(event)

    def finish_session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise DomainNotFound("session", session_id)
        if session["status"] == "finished":
            return copy.deepcopy(session)
        session["status"] = "finished"
        session["finished_at"] = _now()
        self.get_space(session["space_id"])["active_session_id"] = None
        return copy.deepcopy(session)

    def send_message(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        space = self.get_space(space_id)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise DomainConflict("INVALID_REQUEST", "message 不能为空")
        run = self.run("message", status="running")
        message_id = _id("message")
        text = f"当前学习范围包含 {len(space['topic_ids']) or len(self.topics)} 个主题。你的请求是：{message}"
        self.run_service.append_event(run["id"], "message.delta", {"delta": text})
        self.run_service.append_event(run["id"], "message.completed",
                                      {"message_id": message_id, "text": text, "citations": []})
        completed = self.run_service.complete(run["id"], {"type": "message", "id": message_id})
        return {"run_id": run["id"], "session_id": payload.get("session_id"), "status": completed["status"]}

    def create_correction(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.get_space(space_id)
        correction = {"id": _id("correction"), "space_id": space_id, **payload, "status": "pending", "created_at": _now()}
        self.corrections[correction["id"]] = correction
        return copy.deepcopy(correction)

    def confirm_correction(self, correction_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        correction = self.corrections.get(correction_id)
        if correction is None:
            raise DomainNotFound("correction", correction_id)
        correction["status"] = "confirmed"
        run = self.run("knowledge_publish", {"type": "correction", "id": correction_id})
        correction["run_id"] = run["id"]
        return {"run_id": run["id"], "status": "processing", "graph_version": 2, "affected_topic_ids": [], "update_available": True}

    def knowledge_updates(self, space_id: str) -> dict[str, Any]:
        space = self.get_space(space_id)
        return {"space_id": space_id, "bindings": copy.deepcopy(space["bindings"]), "available_updates": [], "affected_topic_ids": [], "invalidated_question_ids": [], "plan_impact": None}

    def apply_knowledge_updates(self, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.space_service.repository.transaction():
            space = self.space_service.repository.get(space_id)
            expected = payload.get("expected_space_version")
            if expected is not None and expected != space["space_version"]:
                raise DomainConflict("VERSION_CONFLICT", "学习空间版本已变化")
            space["space_version"] += 1
            self.space_service.repository.put(space)
            new_version = space["space_version"]
        run = self.run("knowledge_update_apply", {"type": "learning_space", "id": space_id})
        return {"run_id": run["id"], "space_id": space_id, "space_version": new_version, "affected_topic_ids": [], "stale_state_count": 0, "plan_replan_run_id": None}

    def reset_state(self, space_id: str, topic_ids: list[str], reason: str) -> dict[str, Any]:
        space = self.get_space(space_id)
        for topic_id in topic_ids:
            space["state"].pop(topic_id, None)
        space["state_version"] += 1
        self.changes.append({"kind": "state_reset", "space_id": space_id, "topic_ids": topic_ids, "reason": reason, "created_at": _now()})
        return self.get_state(space_id)

    def create_export(self, space_id: str) -> dict[str, Any]:
        space = self.get_space(space_id)
        export_id = _id("export")
        payload = {"space": copy.deepcopy(space), "evidence": self.evidence_for(space_id), "created_at": _now()}
        self.exports[export_id] = {"id": export_id, "space_id": space_id, "status": "ready", "payload": payload, "expires_at": _now()}
        run = self.run("export", {"type": "export", "id": export_id})
        return {"run_id": run["id"], "export_id": export_id, "status": "processing"}

    def export_payload(self, export_id: str) -> dict[str, Any]:
        export = self.exports.get(export_id)
        if export is None:
            raise DomainNotFound("export", export_id)
        return copy.deepcopy(export["payload"])

    def delete_space(self, space_id: str) -> dict[str, Any]:
        self.get_space(space_id)
        self.space_service.delete(space_id)
        self.spaces.pop(space_id, None)
        return {"status": "succeeded", "space_id": space_id}

    def changes_for(self, space_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in self.changes if item.get("space_id") == space_id]
