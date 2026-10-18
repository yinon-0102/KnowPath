from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository, MaterialParser
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.errors import DomainConflict


@pytest.mark.parametrize('mode', ['normal', 'stale', 'old_event', 'archived', 'superseded', 'historical', 'reason', 'key'])
def test_undo_completion_preserves_facts_and_exact_event(backend, mode):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    session = state.start_session(plan['id'], task['id'])
    state.finish_session(session['id'])
    linked_practice(state, space_id, plan, task, complete=True)
    done = state.update_task(plan['id'], task['id'], {'status': 'completed', 'expected_plan_version': 1, 'note': 'original note'}, idempotency_key='e1')
    original = done['context']['completion']
    assert original.get('event_id'), 'completion needs stable event identity'
    record = state.get_learning_record(plan['id'], task['id'])
    evidence, mastery = state.evidence_for(space_id), state.get_state(space_id)
    body = {'expected_plan_version': 2, 'completion_event_id': original['event_id'], 'reason': 'clicked by mistake'}
    key, expected_error = 'undo-e1', None
    if mode == 'stale': body['expected_plan_version'] = 1; expected_error = 'VERSION_CONFLICT'
    if mode == 'old_event': body['completion_event_id'] = 'wrong'; expected_error = 'COMPLETION_NOT_CURRENT'
    if mode == 'archived':
        state.update_space(space_id, {'status': 'archived', 'expected_version': state.get_space(space_id)['space_version']})
        expected_error = 'SPACE_ARCHIVED'
    if mode == 'superseded': state.create_plan(space_id, {}); expected_error = 'PLAN_NOT_ACTIVE'
    if mode == 'historical':
        if state.plan_sessions.sql:
            with state.plan_sessions._tx():
                _, tasks = state.plan_sessions._get_plan_sql(plan['id'], lock=True)
                row = next(t for t in tasks if t.id == task['id']); row.context = {**row.context, 'historical': True}
        else: state.plan_sessions._load_plan_memory(plan['id'])['tasks'][0]['context']['historical'] = True
        expected_error = 'TASK_NOT_ACTIVE'
    if mode == 'reason': body['reason'] = ' '; expected_error = 'UNDO_REASON_REQUIRED'
    if mode == 'key': key = None; expected_error = 'IDEMPOTENCY_KEY_REQUIRED'
    if expected_error:
        with pytest.raises(DomainConflict) as exc: state.undo_task_completion(plan['id'], task['id'], body, idempotency_key=key)
        assert exc.value.code == expected_error
        assert state.get_plan(plan['id'])['tasks'][0]['status'] == 'completed'
        return
    undo = state.undo_task_completion(plan['id'], task['id'], body, idempotency_key=key)
    assert undo['status'] == 'pending' and undo['note'] == 'original note'
    assert undo['plan_version'] == 3
    assert undo['context']['completion_history'][-1]['completion'] == original
    assert undo['context']['completion_history'][-1]['reason'] == body['reason']
    restored = LearningState(backend)
    assert restored.undo_task_completion(plan['id'], task['id'], body, idempotency_key=key) == undo
    after = restored.get_learning_record(plan['id'], task['id'])
    for field in ['sessions', 'assessments', 'note']:
        assert after.get(field) == record.get(field)
    assert restored.evidence_for(space_id) == evidence and restored.get_state(space_id) == mastery
    new_session = restored.start_session(plan['id'], task['id'], idempotency_key='new-session')
    assert new_session['id'] != session['id']
    e2 = restored.update_task(plan['id'], task['id'], {'status': 'completed', 'expected_plan_version': 3}, idempotency_key='e2')
    assert e2['context']['completion']['event_id'] != original['event_id']
    assert restored.undo_task_completion(plan['id'], task['id'], body, idempotency_key=key) == undo
    with pytest.raises(DomainConflict) as exc:
        restored.undo_task_completion(plan['id'], task['id'], {**body, 'expected_plan_version': 4}, idempotency_key='new-undo-old-event')
    assert exc.value.code == 'COMPLETION_NOT_CURRENT'
    assert restored.get_plan(plan['id'])['tasks'][0]['status'] == 'completed'


def seed(repo, *, legacy=False):
    state = LearningState(repo)
    if legacy:
        class LegacyParser(MaterialParser):
            VERSION = 'knowpath_parser_v1'
            def parse(self, content, filename):
                return self._parse_text(content.decode())
        state.material_service.parser = LegacyParser()
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nFunctions group reusable behavior.\n\n# Loops\n\nLoops repeat work.", idempotency_key="seed-material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id], "weekly_minutes": 120})
    if legacy:
        # Historical pre-default spaces used an empty scope as the full legacy
        # projection (including aliases). Preserve those frozen-plan fixtures.
        historical = state.space_service.repository.get(space['id'])
        historical['topic_ids'] = []
        historical.pop('scope_mode', None)
        state.space_service.repository.put(historical)
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
        state.create_plan(space_id, {"schedule_mode": "session_window"})
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


def test_http_datetime_event_idempotency_and_conflict_details(monkeypatch):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    # Two genuine source units make the 15-minute budget unsatisfiable; aliases
    # of one unit must not create artificial workload under the new parser.
    material = state.material_service.create(filename="api.txt", content=b"API contract example\n\n# Responses\n\nResponses describe the operation result.", idempotency_key="api-source")
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
    blocked = client.post(f"/api/v1/plans/{plan['id']}/sessions", json={"task_id": plan["tasks"][0]["id"]}, headers={"Idempotency-Key": "deferred-start"})
    assert blocked.status_code == 409
    import knowpath_backend.learning.plans.service as planner
    monkeypatch.setattr(planner, "now", lambda: "2099-01-01T00:00:00+00:00")
    rebuilt = client.post(url, json={"rebuild_mode": "local_replan", "base_plan_id": plan["id"],
        "expected_plan_version": 2}, headers={"Idempotency-Key": "resume-plan"})
    assert rebuilt.status_code == 202, rebuilt.text
    plan = state.get_plan(rebuilt.json()["plan_id"])
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
    constraint = client.post(url, json={"schedule_mode": "session_window"}, headers={"Idempotency-Key": "api-constraint"})
    assert constraint.status_code == 422
    assert constraint.json()["error"]["details"]["conflicts"]


def test_planning_uses_actual_minutes_and_deadline(monkeypatch):
    import knowpath_backend.learning.plans.service as planner
    monkeypatch.setattr(planner, "now", lambda: "2026-09-18T10:00:00+00:00")
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    state.set_scope(space_id, {"topic_ids": [topics[0]["id"]], "expected_version": 2})
    state.update_profile(space_id, {"weekly_minutes": 15, "expected_version": 1})
    plan = state.create_plan(space_id, {"schedule_mode": "session_window"})
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
    restored = LearningState(backend).get_plan(plan["id"])
    assert restored["status"] == "ready"
    assert restored["adjustment_pending"]
    assert state.start_session(plan["id"], plan["tasks"][0]["id"])["status"] == "active"


def test_due_review_and_weak_topics_get_specific_tasks(monkeypatch):
    import knowpath_backend.learning.plans.service as planner
    monkeypatch.setattr(planner, "now", lambda: "2026-09-18T10:00:00+00:00")
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    # A canonical unit and its compatibility alias are one new-parser unit.
    topics = [topic for topic in topics if topic['id'] == topic.get('canonical_topic_id', topic['id'])]
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


def linked_practice(state, space_id, plan, task, *, complete=False):
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    state.assessment_service.generator = FixedQuestions()
    created = state.create_assessment(space_id, {"kind": "practice", "plan_id": plan["id"],
        "task_id": task["id"], "question_count": 5,
        "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
    if complete:
        state.record_attempt(created["id"], {"answers": [{"question_id": q["id"], "answer": "A",
            "expected_answer_revision": 0} for q in state.get_assessment(created["id"])["questions"]], "finalize": True})
    return created


def test_full_score_waits_for_explicit_task_confirmation_and_advances_default(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    first, second = plan["tasks"][:2]
    measured = linked_practice(state, space_id, plan, first, complete=True)
    assert all(row["verdict"] == "correct" for row in state.assessment_result(measured["id"])["question_results"])
    states_before = state.assessment_service.repository.records("states", space_id=space_id, lock=False)
    evidence_before = state.evidence_for(space_id)
    assert state.get_plan(plan["id"])["tasks"][0]["status"] == "pending"
    result = state.update_task(plan["id"], first["id"], {"expected_plan_version": plan["version"],
        "status": "completed", "source": "assessment", "assessment_id": measured["id"]},
        idempotency_key="confirm-first")
    assert result["context"]["completion"]["source"] == "assessment"
    assert result["context"]["completion"]["assessment_id"] == measured["id"]
    assert result["next_action"] == {"kind": "task", "plan_id": plan["id"], "task_id": second["id"]}
    assert state.get_workbench(space_id)["round_summary"]["tasks"]["completed"] == 1
    assert state.assessment_service.repository.records("states", space_id=space_id, lock=False) == states_before
    assert state.evidence_for(space_id) == evidence_before


@pytest.mark.parametrize("invalid", ["unfinished", "other_task", "other_space", "unlinked"])
def test_assessment_completion_requires_exact_completed_association(backend, invalid):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    if invalid == "other_space":
        other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
        other_plan = state.create_plan(other["id"], {})
        measured = linked_practice(state, other["id"], other_plan, other_plan["tasks"][0], complete=True)
    elif invalid == "unlinked":
        from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
        state.assessment_service.generator = FixedQuestions()
        measured = state.create_assessment(space_id, {"kind": "practice", "question_count": 5,
            "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
        state.finalize_assessment(measured["id"], {"allow_unanswered": True})
    else:
        measured = linked_practice(state, space_id, plan,
            plan["tasks"][1] if invalid == "other_task" else task, complete=invalid != "unfinished")
    before = state.get_plan(plan["id"])
    with pytest.raises(DomainConflict) as exc:
        state.update_task(plan["id"], task["id"], {"expected_plan_version": before["version"],
            "status": "completed", "source": "assessment", "assessment_id": measured["id"]}, idempotency_key="invalid")
    assert exc.value.code == ("ASSESSMENT_NOT_COMPLETED" if invalid == "unfinished" else "ASSESSMENT_TASK_MISMATCH")
    assert state.get_plan(plan["id"])["version"] == before["version"]
    assert state.get_plan(plan["id"])["tasks"][0]["status"] == "pending"


def test_manual_completion_is_durable_idempotent_and_never_adds_measurement(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    body = {"expected_plan_version": 1, "status": "completed"}
    result = state.update_task(plan["id"], task["id"], body, idempotency_key="manual-confirm")
    restored = LearningState(backend)
    assert restored.update_task(plan["id"], task["id"], body, idempotency_key="manual-confirm") == result
    assert result["context"]["completion"]["source"] == "manual"
    assert "assessment_id" not in result["context"]["completion"]
    assert restored.get_state(space_id)["state_version"] == 0
    assert restored.evidence_for(space_id) == []
    with pytest.raises(DomainConflict) as exc:
        restored.update_task(plan["id"], plan["tasks"][1]["id"], body, idempotency_key="manual-confirm")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert restored.get_plan(plan["id"])["tasks"][1]["status"] == "pending"


def test_last_confirmation_returns_summary_and_counts_skips_and_deferrals_separately(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    for index, task in enumerate(plan["tasks"]):
        body = {"expected_plan_version": index + 1, "status": "skipped" if index == 0 else "deferred"}
        body.update({"reason": "暂不学习"} if index == 0 else {"defer_until": "2099-01-01T00:00:00+00:00"})
        result = state.update_task(plan["id"], task["id"], body, idempotency_key=f"process-{index}")
    assert result["next_action"] == {"kind": "summary", "plan_id": plan["id"]}
    summary = LearningState(backend).get_workbench(space_id)["round_summary"]
    assert summary["processed"] is True
    assert summary["all_completed"] is False
    assert summary["tasks"] == {"total": len(plan["tasks"]), "completed": 0, "skipped": 1,
        "deferred": len(plan["tasks"]) - 1, "remaining": 0}
    assert summary["assessments"]["completed"] == 0
    assert summary["assessments"]["unverified"] == 0
    assert summary["mastery"]["mastered"] == 0
    assert summary["review"]["due"] == 0


def test_assessment_confirmation_locks_assessment_before_space_and_plan(backend, monkeypatch):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    measured = linked_practice(state, space_id, plan, task, complete=True)
    seen = []
    read = state.plan_sessions.repository.get_record
    get_space = state.plan_sessions.spaces.repository.get
    def record(table, identifier, **kwargs):
        if table == "assessments" and kwargs.get("lock", True):
            seen.append("assessment")
        return read(table, identifier, **kwargs)
    def space(identifier):
        seen.append("space")
        return get_space(identifier)
    monkeypatch.setattr(state.plan_sessions.repository, "get_record", record)
    monkeypatch.setattr(state.plan_sessions.spaces.repository, "get", space)
    state.update_task(plan["id"], task["id"], {"status": "completed", "source": "assessment",
        "assessment_id": measured["id"], "expected_plan_version": plan["version"]}, idempotency_key="lock-order")
    assert seen[0] == "assessment"
    assert seen.count("assessment") == 1


def test_all_tasks_confirmed_show_summary_without_claiming_manual_mastery(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    for index, task in enumerate(plan["tasks"]):
        result = state.update_task(plan["id"], task["id"], {"status": "completed", "expected_plan_version": index + 1},
            idempotency_key=f"complete-{index}")
    assert result["next_action"] == {"kind": "summary", "plan_id": plan["id"]}
    summary = state.get_workbench(space_id)["round_summary"]
    assert summary["processed"] and summary["all_completed"]
    assert summary["tasks"]["completed"] == len(plan["tasks"])
    assert summary["assessments"] == {"completed": 0, "unverified": 0}
    assert summary["mastery"]["mastered"] == 0


def test_task_completion_http_source_and_replay_contract():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename="confirm.md", content=b"# One\n\nFirst topic.\n\n# Two\n\nSecond topic.", idempotency_key="confirm-source")
    space = state.create_space({"name": "Confirm", "material_ids": [material.material.id]})
    plan = state.create_plan(space["id"], {})
    task = plan["tasks"][0]
    measured = linked_practice(state, space["id"], plan, task, complete=True)
    body = {"expected_plan_version": plan["version"], "status": "completed", "source": "assessment", "assessment_id": measured["id"]}
    path = f"/api/v1/plans/{plan['id']}/tasks/{task['id']}"
    with TestClient(app) as client:
        missing = client.patch(path, json=body)
        assert missing.status_code == 400, missing.text
        assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        first = client.patch(path, json=body, headers={"Idempotency-Key": "http-confirm"})
        assert first.status_code == 200, first.text
        assert first.json()["context"]["completion"]["assessment_id"] == measured["id"]
        assert client.patch(path, json=body, headers={"Idempotency-Key": "http-confirm"}).json() == first.json()
        different = client.patch(path, json={**body, "expected_plan_version": 2}, headers={"Idempotency-Key": "http-confirm"})
        assert different.status_code == 409
        malformed = client.patch(path, json={**body, "source": "manual"}, headers={"Idempotency-Key": "bad-source"})
        assert malformed.status_code == 422


def test_completion_cannot_modify_archived_space(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    space = state.get_space(space_id)
    state.update_space(space_id, {"status": "archived", "expected_version": space["space_version"]})
    with pytest.raises(DomainConflict) as exc:
        state.update_task(plan["id"], plan["tasks"][0]["id"], {"status": "completed", "expected_plan_version": 1}, idempotency_key="archived-complete")
    assert exc.value.code == "SPACE_ARCHIVED"

@pytest.mark.parametrize('with_frozen_completion', [True, False])
def test_undo_legacy_completion_identity_is_note_independent_and_read_back(backend, with_frozen_completion):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task_id = plan['tasks'][0]['id']
    original = {'source': 'manual', 'confirmed_at': '2025-01-01T00:00:00+00:00'}
    if state.plan_sessions.sql:
        with state.plan_sessions._tx():
            _, tasks = state.plan_sessions._get_plan_sql(plan['id'], lock=True)
            row = next(t for t in tasks if t.id == task_id)
            row.status, row.note = 'completed', 'legacy note'
            row.context = {**row.context, **({'completion': original} if with_frozen_completion else {})}
    else:
        row = state.plan_sessions._load_plan_memory(plan['id'])['tasks'][0]
        row.update(status='completed', note='legacy note')
        if with_frozen_completion: row['context']['completion'] = original
    mapped = state.get_plan(plan['id'])['tasks'][0]['context']['completion']
    assert mapped['event_id'].startswith('legacy:')
    assert LearningState(backend).get_plan(plan['id'])['tasks'][0]['context']['completion'] == mapped
    changed = state.update_task(plan['id'], task_id, {'status': 'completed', 'note': 'edited note', 'expected_plan_version': 1})
    assert changed['context']['completion']['event_id'] == mapped['event_id']
    body = {'completion_event_id': mapped['event_id'], 'expected_plan_version': 2, 'reason': 'legacy mistake'}
    undone = state.undo_task_completion(plan['id'], task_id, body, idempotency_key='legacy-undo')
    assert undone['note'] == 'edited note'
    assert undone['context']['completion_history'][0]['completion'] == mapped
    restored = LearningState(backend)
    assert restored.undo_task_completion(plan['id'], task_id, body, idempotency_key='legacy-undo') == undone
    for altered in [{**body, 'reason': 'different reason'}, {**body, 'expected_plan_version': 3}, {**body, 'completion_event_id': 'other'}]:
        with pytest.raises(DomainConflict) as exc:
            restored.undo_task_completion(plan['id'], task_id, altered, idempotency_key='legacy-undo')
        assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


def test_undo_preserves_active_session_and_refuses_a_second_session(backend):
    state, space_id, _ = seed(backend)
    plan = state.create_plan(space_id, {})
    task_id = plan['tasks'][0]['id']
    session = state.start_session(plan['id'], task_id)
    completed = state.update_task(plan['id'], task_id, {'status': 'completed', 'expected_plan_version': 1})
    before = state.get_workbench(space_id)['active_session']
    body = {'expected_plan_version': 2, 'completion_event_id': completed['context']['completion']['event_id'], 'reason': 'mistake'}
    state.undo_task_completion(plan['id'], task_id, body, idempotency_key='active-undo')
    after = state.get_workbench(space_id)['active_session']
    # Workbench projects a running activity clock; compare its immutable facts.
    for field in ['session_id', 'task_id', 'plan_id', 'status', 'started_at', 'finished_at']:
        assert after.get(field) == before.get(field)
    assert {k: v for k, v in after['context'].items() if k != 'activity'} == {k: v for k, v in before['context'].items() if k != 'activity'}
    with pytest.raises(DomainConflict) as exc: state.start_session(plan['id'], task_id)
    assert exc.value.code == 'SESSION_ACTIVE'
    assert state.finish_session(session['id'])['status'] == 'finished'


def test_undo_rejects_invalidated_plan_without_changing_completion(backend):
    state, space_id, topics = seed(backend)
    plan = state.create_plan(space_id, {})
    task_id = plan['tasks'][0]['id']
    completed = state.update_task(plan['id'], task_id, {'status': 'completed', 'expected_plan_version': 1})
    state.set_scope(space_id, {'topic_ids': [topics[-1]['id']], 'expected_version': state.get_space(space_id)['space_version']})
    assert state.get_plan(plan['id'])['status'] == 'needs_replan'
    with pytest.raises(DomainConflict) as exc:
        state.undo_task_completion(plan['id'], task_id, {'expected_plan_version': 2,
            'completion_event_id': completed['context']['completion']['event_id'], 'reason': 'mistake'}, idempotency_key='invalidated-undo')
    assert exc.value.code == 'PLAN_NOT_ACTIVE'
    assert state.get_plan(plan['id'])['tasks'][0]['status'] == 'completed'


def test_undo_http_strict_confirmation_contract():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename='undo.md', content=b'# One\n\nFirst topic.\n\n# Two\n\nSecond topic.', idempotency_key='undo-source')
    space = state.create_space({'name': 'Undo', 'material_ids': [material.material.id]})
    plan = state.create_plan(space['id'], {})
    task_id = plan['tasks'][0]['id']
    completed = state.update_task(plan['id'], task_id, {'status': 'completed', 'expected_plan_version': 1, 'note': 'keep'})
    url = f"/api/v1/plans/{plan['id']}/tasks/{task_id}/undo-completion"
    body = {'expected_plan_version': 2, 'completion_event_id': completed['context']['completion']['event_id'], 'reason': 'mistake'}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 400
        for invalid in [{**body, 'reason': ' '}, {**body, 'completion_event_id': ''}, {**body, 'expected_plan_version': '2'}, {**body, 'note': 'not identity'}]:
            assert client.post(url, json=invalid, headers={'Idempotency-Key': 'invalid'}).status_code == 422
        first = client.post(url, json=body, headers={'Idempotency-Key': 'http-undo'})
        assert first.status_code == 200, first.text
        assert first.json()['status'] == 'pending' and first.json()['note'] == 'keep'
        assert first.json()['task_progress']['completed'] == 0 and first.json()['task_progress']['total'] > 0
        assert client.post(url, json=body, headers={'Idempotency-Key': 'http-undo'}).json() == first.json()
        assert client.post(url, json={**body, 'reason': 'edited'}, headers={'Idempotency-Key': 'http-undo'}).status_code == 409
