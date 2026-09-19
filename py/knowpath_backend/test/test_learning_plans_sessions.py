from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.db import init_db
from knowpath_backend.learning.materials import InMemoryMaterialRepository
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.errors import DomainConflict


def seed(repo):
    state = LearningState(repo)
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nFunctions group reusable behavior.\n\n# Loops\n\nLoops repeat work.", idempotency_key="seed-material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id], "weekly_minutes": 120})
    topics = state.topics_for_space(space["id"])
    state.set_scope(space["id"], {"topic_ids": [t["id"] for t in topics], "expected_version": 1})
    return state, space["id"], topics


def test_sql_plan_and_session_survive_restart(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learning.db'}")
    init_db(engine)
    first, space_id, topics = seed(SqlAlchemyMaterialRepository(engine))
    plan = first.create_plan(space_id, {"session_count": 3, "minutes_per_session": 30})
    assert plan["status"] == "ready"
    assert plan["tasks"]
    second = LearningState(SqlAlchemyMaterialRepository(engine))
    restored = second.get_plan(plan["plan_id"])
    assert restored["plan_id"] == plan["plan_id"]
    session = second.start_session(plan["plan_id"], plan["tasks"][0]["id"])
    event = second.add_session_event(session["id"], {"type": "open_material", "event_id": "evt-1"})
    assert event["type"] == "open_material"
    assert second.add_session_event(session["id"], {"type": "open_material", "event_id": "evt-1"})["id"] == event["id"]
    finished = second.finish_session(session["id"])
    assert finished["status"] == "finished"
    third = LearningState(SqlAlchemyMaterialRepository(engine))
    assert third.finish_session(session["id"])["status"] == "finished"


def test_only_one_active_session_and_plan_version_conflict():
    state, space_id, _ = seed(InMemoryMaterialRepository())
    plan = state.create_plan(space_id, {})
    with pytest.raises(DomainConflict) as exc:
        state.update_task(plan["plan_id"], plan["tasks"][0]["id"], {"expected_plan_version": 99, "status": "completed"})
    assert exc.value.code == "VERSION_CONFLICT"
    first = state.start_session(plan["plan_id"], plan["tasks"][0]["id"])
    with pytest.raises(DomainConflict) as exc:
        state.start_session(plan["plan_id"], plan["tasks"][0]["id"])
    assert exc.value.code == "SESSION_ACTIVE"
    state.finish_session(first["id"])


def test_task_validation_and_local_replan_preserve_history():
    state, space_id, _ = seed(InMemoryMaterialRepository())
    plan = state.create_plan(space_id, {"session_count": 3, "minutes_per_session": 30})
    task = plan["tasks"][0]
    state.update_task(plan["plan_id"], task["id"], {"expected_plan_version": 1, "status": "completed", "note": "done"})
    replanned = state.create_plan(space_id, {"session_count": 3, "minutes_per_session": 30, "rebuild_mode": "local_replan", "base_plan_id": plan["plan_id"], "expected_plan_version": 2})
    assert state.get_plan(plan["plan_id"])["status"] == "superseded"
    assert any(t["status"] == "completed" for t in replanned["tasks"])
    with pytest.raises(DomainConflict) as exc:
        state.update_task(replanned["plan_id"], replanned["tasks"][0]["id"], {"expected_plan_version": replanned["version"], "status": "skipped"})
    assert exc.value.code == "SKIP_REASON_REQUIRED"


def test_plan_and_session_idempotency_survive_recreation(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'replay.db'}")
    init_db(engine)
    first, space_id, _ = seed(SqlAlchemyMaterialRepository(engine))
    plan = first.create_plan(space_id, {}, idempotency_key="plan-create")
    second = LearningState(SqlAlchemyMaterialRepository(engine))
    assert second.create_plan(space_id, {}, idempotency_key="plan-create") == plan
    with pytest.raises(DomainConflict, match="幂等"):
        second.create_plan(space_id, {"minutes_per_session": 10}, idempotency_key="plan-create")
    session = first.start_session(plan["id"], plan["tasks"][0]["id"], idempotency_key="session-create")
    assert second.start_session(plan["id"], plan["tasks"][0]["id"], idempotency_key="session-create") == session
    event = first.add_session_event(session["id"], {"type": "pause", "event_id": "pause"})
    finished = second.finish_session(session["id"])
    assert first.finish_session(session["id"]) == finished
    assert first.add_session_event(session["id"], {"type": "pause", "event_id": "pause"}) == event
    with pytest.raises(DomainConflict) as exc:
        second.add_session_event(session["id"], {"type": "resume", "event_id": "pause"})
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert second.get_space(space_id)["active_session_id"] is None
    engine.dispose()


def test_memory_restart_and_foreign_plan_replan():
    repository = InMemoryMaterialRepository()
    first, space_id, _ = seed(repository)
    plan = first.create_plan(space_id, {})
    second = LearningState(repository)
    assert second.get_plan(plan["id"])["tasks"] == plan["tasks"]
    other = second.create_space({"name": "Other", "material_ids": [first.get_space(space_id)["bindings"][0]["material_id"]]})
    with pytest.raises(DomainConflict) as exc:
        second.create_plan(other["id"], {"rebuild_mode": "local_replan", "base_plan_id": plan["id"], "expected_plan_version": 1})
    assert exc.value.code == "PLAN_SPACE_MISMATCH"


def test_budget_and_overflow_return_actionable_conflicts():
    state, space_id, topics = seed(InMemoryMaterialRepository())
    state.update_profile(space_id, {"weekly_minutes": 15, "expected_version": 1})
    with pytest.raises(DomainConflict) as exc:
        state.create_plan(space_id, {})
    assert exc.value.code == "PLAN_CONSTRAINT_UNSATISFIABLE"
    assert exc.value.details["conflicts"]
    assert exc.value.details["adjustable_constraints"]


def test_version_conflict_contains_latest_plan_and_no_mastery_write():
    state, space_id, _ = seed(InMemoryMaterialRepository())
    plan = state.create_plan(space_id, {})
    state.update_task(plan["id"], plan["tasks"][0]["id"], {"expected_plan_version": 1, "status": "completed"})
    with pytest.raises(DomainConflict) as exc:
        state.update_task(plan["id"], plan["tasks"][0]["id"], {"expected_plan_version": 1, "status": "completed"})
    assert exc.value.details["latest_plan"]["version"] == 2
    assert state.get_state(space_id)["state_version"] == 0
    assert state.evidence_for(space_id) == []


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    if request.param == "memory":
        yield InMemoryMaterialRepository()
    else:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'contracts.db'}")
        init_db(engine)
        yield SqlAlchemyMaterialRepository(engine)
        engine.dispose()


def test_active_session_survives_restart_and_releases_slot(backend):
    first, space_id, _ = seed(backend)
    plan = first.create_plan(space_id, {})
    started = first.start_session(plan["id"], plan["tasks"][0]["id"])
    second = LearningState(backend)
    assert second.get_space(space_id)["active_session_id"] == started["id"]
    with pytest.raises(DomainConflict) as exc:
        second.start_session(plan["id"], plan["tasks"][0]["id"])
    assert exc.value.code == "SESSION_ACTIVE"
    second.finish_session(started["id"])
    next_session = first.start_session(plan["id"], plan["tasks"][0]["id"])
    assert next_session["id"] != started["id"]
    assert next_session["context"]["topic_ids"] == plan["tasks"][0]["topic_ids"]
    assert next_session["context"]["source_refs"]
    assert "answer_key" not in str(next_session)


def test_local_replan_preserves_unchanged_tasks_and_history(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    changed = state.update_task(plan["id"], task["id"], {"status": "deferred", "expected_plan_version": 1,
                               "defer_until": "2099-01-01T00:00:00+00:00", "note": "later"})
    new = state.create_plan(space_id, {"rebuild_mode": "local_replan", "base_plan_id": plan["id"], "expected_plan_version": 2})
    copied = next(t for t in new["tasks"] if t["topic_ids"] == task["topic_ids"])
    assert copied["status"] == "deferred"
    assert copied["defer_until"] == changed["defer_until"]
    assert copied["context"]["origin_task_id"] == task["id"]
    assert LearningState(backend).get_plan(plan["id"])["status"] == "superseded"
    assert LearningState(backend).get_plan(new["id"])["tasks"] == new["tasks"]
    with pytest.raises(DomainConflict) as exc:
        state.start_session(plan["id"], plan["tasks"][1]["id"])
    assert exc.value.code == "PLAN_NOT_ACTIVE"


def test_http_datetime_event_idempotency_and_conflict_details():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename="api.txt", content=b"API contract example", idempotency_key="api-source")
    space = state.create_space({"name": "API", "material_ids": [material.material.id]})
    client = TestClient(app)
    url = f"/api/v1/learning-spaces/{space['id']}/plans"
    assert client.post(url, json={}).status_code == 400
    created = client.post(url, json={}, headers={"Idempotency-Key": "api-plan"})
    assert created.status_code == 202
    plan = state.get_plan(created.json()["plan_id"])
    task_url = f"/api/v1/plans/{plan['id']}/tasks/{plan['tasks'][0]['id']}"
    update = client.patch(task_url, json={"status": "deferred", "expected_plan_version": 1, "defer_until": "2099-01-01T00:00:00Z"})
    assert update.status_code == 200, update.text
    conflict = client.patch(task_url, json={"status": "completed", "expected_plan_version": 1})
    assert conflict.json()["error"]["details"]["latest_plan"]["version"] == 2
    assert client.patch(task_url, json={"status": "skipped", "expected_plan_version": 2, "note": "no reason"}).status_code == 422
    started = client.post(f"/api/v1/plans/{plan['id']}/sessions", json={"task_id": plan["tasks"][0]["id"]}, headers={"Idempotency-Key": "api-start"})
    assert started.status_code == 201, started.text
    events = f"/api/v1/sessions/{started.json()['id']}/events"
    body = {"type": "pause", "client_timestamp": "2026-01-01T00:00:00Z"}
    event = client.post(events, json=body, headers={"Idempotency-Key": "api-event"})
    assert event.status_code == 201, event.text
    assert client.post(events, json=body, headers={"Idempotency-Key": "api-event"}).json() == event.json()
    assert client.post(events, json=body).status_code == 400
    assert client.post(events, json={"type": "resume"}, headers={"Idempotency-Key": "api-event"}).status_code == 409
    state.update_profile(space["id"], {"weekly_minutes": 15, "expected_version": 1})
    constraint = client.post(url, json={}, headers={"Idempotency-Key": "api-constraint"})
    assert constraint.status_code == 422
    assert constraint.json()["error"]["details"]["conflicts"]


def test_planning_uses_actual_minutes_and_deadline(monkeypatch):
    import knowpath_backend.learning.planner as planner
    monkeypatch.setattr(planner, "now", lambda: "2026-09-18T10:00:00+00:00")
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    state.set_scope(space_id, {"topic_ids": [topics[0]["id"]], "expected_version": 2})
    state.update_profile(space_id, {"weekly_minutes": 15, "expected_version": 1})
    plan = state.create_plan(space_id, {})
    assert sum(t["estimated_minutes"] for t in plan["tasks"]) <= 15
    assert plan["tasks"][0]["context"]["scheduled_date"] == "2026-09-18"
    state.set_scope(space_id, {"topic_ids": [t["id"] for t in topics], "expected_version": 3})
    state.update_profile(space_id, {"weekly_minutes": 120, "target_date": "2026-09-18", "expected_version": 2})
    with pytest.raises(DomainConflict) as exc:
        state.create_plan(space_id, {"minutes_per_session": 10})
    assert exc.value.code == "PLAN_CONSTRAINT_UNSATISFIABLE"
    assert "target_date" in exc.value.details["conflicts"]


def test_plan_becomes_stale_after_profile_change(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    state.update_profile(space_id, {"weekly_minutes": 110, "expected_version": 1})
    assert LearningState(backend).get_plan(plan["id"])["status"] == "needs_replan"
    with pytest.raises(DomainConflict) as exc:
        state.start_session(plan["id"], plan["tasks"][0]["id"])
    assert exc.value.code == "PLAN_NOT_ACTIVE"


def test_due_review_and_weak_topics_get_specific_tasks(monkeypatch):
    import knowpath_backend.learning.planner as planner
    monkeypatch.setattr(planner, "now", lambda: "2026-09-18T10:00:00+00:00")
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    for index, topic in enumerate(topics):
        repo.assessment_data["states"][topic["id"]] = {"id": topic["id"], "space_id": space_id, "topic_id": topic["id"],
            "status": "mastered" if index == 0 else "unstable", "state_version": 1,
            "mastery_score": 0.9 if index == 0 else 0.4, "error_tags": [] if index == 0 else ["concept_gap"],
            "last_assessed_at": "2026-09-01T00:00:00+00:00", "next_review_at": "2026-09-08T00:00:00+00:00"}
    plan = state.create_plan(space_id, {})
    tasks = {t["topic_ids"][0]: t for t in plan["tasks"]}
    assert tasks[topics[0]["id"]]["kind"] == "review"
    assert tasks[topics[1]["id"]]["kind"] == "targeted_practice"
    assert "concept_gap" in tasks[topics[1]["id"]]["reason"]
    without_reviews = state.create_plan(space_id, {"include_review": False})
    assert all(t["kind"] != "review" for t in without_reviews["tasks"])


def test_hint_is_atomic_and_excluded_from_independent_evidence(backend, monkeypatch):
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    state, space_id, _ = seed(backend)
    state.assessment_service.generator = FixedQuestions()
    assessment = state.create_assessment(space_id, {"kind": "practice", "question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
    question = state.get_assessment(assessment["id"])["questions"][0]
    plan = state.create_plan(space_id, {})
    session = state.start_session(plan["id"], plan["tasks"][0]["id"])
    remember = state.assessment_service.repository.remember
    def fail_save(*args, **kwargs):
        raise RuntimeError("injected event commit failure")
    monkeypatch.setattr(state.assessment_service.repository, "remember", fail_save)
    with pytest.raises(RuntimeError):
        state.add_session_event(session["id"], {"type": "request_hint", "question_id": question["id"], "event_id": "hint-failure"})
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    assert not next(q for q in stored["questions"] if q["id"] == question["id"]).get("assisted")
    monkeypatch.setattr(state.assessment_service.repository, "remember", remember)
    event = state.add_session_event(session["id"], {"type": "request_hint", "question_id": question["id"], "event_id": "hint-success"})
    state.finish_session(session["id"])
    assert LearningState(backend).add_session_event(session["id"], {"type": "request_hint", "question_id": question["id"], "event_id": "hint-success"}) == event
    second_question = state.get_assessment(assessment["id"])["questions"][1]
    with pytest.raises(DomainConflict) as exc:
        state.add_session_event(session["id"], {"type": "request_hint", "question_id": second_question["id"], "event_id": "hint-late"})
    assert exc.value.code == "SESSION_FINISHED"
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    assert not next(q for q in stored["questions"] if q["id"] == second_question["id"]).get("assisted")
    state.record_attempt(assessment["id"], {"answers": [{"question_id": question["id"], "answer": "A", "expected_answer_revision": 0}]})
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    evidence = state.evidence_for(space_id)
    hinted = [e for e in evidence if e["question_id"] == question["id"]]
    assert hinted and all(e["assisted"] and not e["eligible"] for e in hinted)
