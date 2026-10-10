"""Workbench reads preserve node identity, ownership and frozen learning history."""
import json

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
from knowpath_backend.test.test_learning_plan_cycles import save_state, replan


@pytest.fixture(params=["memory", "sqlite"])
def workbench(request, tmp_path, monkeypatch):
    import knowpath_backend.learning.plans.service as planner
    clock = ["2026-09-01T10:00:00+00:00"]
    monkeypatch.setattr(planner, "now", lambda: clock[0])
    engine = None
    memory = InMemoryMaterialRepository()
    if request.param == "sqlite":
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'workbench.db'}")
        init_db(engine)
    def factory():
        return LearningState(SqlAlchemyMaterialRepository(engine) if engine else memory,
                             question_generator=FixedQuestions())
    state = factory()
    upload = state.material_service.create(filename="notes.md", content=b"# Functions\n\nFunctions group reusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Python", "material_ids": [upload.material.id]})
    topic = state.topics_for_space(space["id"])[0]
    state.set_scope(space["id"], {"topic_ids": [topic["id"]], "expected_version": 1})
    yield factory, state, space["id"], topic, clock
    if engine:
        engine.dispose()


def assessment(state, space_id, **links):
    return state.create_assessment(space_id, {"kind": "retest", "question_count": 5,
        "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}, **links})


def test_empty_workbench_and_progress(workbench):
    _, state, space_id, _, _ = workbench
    assert state.get_workbench(space_id) == {"space_id": space_id, "current_plan": None, "active_session": None}
    assert state.get_progress(space_id) == {"space_id": space_id, "rounds": [], "completed_count": 0,
                                           "total_count": 0, "next_cursor": None}


def test_restart_restores_paused_active_session_and_stable_task_order(workbench):
    factory, state, space_id, topic, clock = workbench
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    assert task["node_id"] == task["context"]["node_id"]
    assert task["sequence"] == task["context"]["sequence"] == 1
    session = state.start_session(plan["id"], task["id"])
    clock[0] = "2026-09-01T10:01:00+00:00"
    state.add_session_event(session["id"], {"type": "pause", "event_id": "pause"})
    restored = factory().get_workbench(space_id)
    assert restored["current_plan"]["tasks"][0]["node_id"] == task["node_id"]
    assert restored["active_session"]["space_id"] == space_id
    assert restored["active_session"]["session_id"] == session["id"]
    assert restored["active_session"]["paused"] is True
    node = factory().get_progress(space_id)["rounds"][0]["nodes"][0]
    assert node["status"] == "active"
    assert node["active_session_id"] == session["id"]
    assert node["title"] == topic["name"]


def test_inherited_node_aggregates_sessions_across_plans_without_double_counting(workbench):
    factory, state, space_id, topic, clock = workbench
    first = state.create_plan(space_id, {})
    original = first["tasks"][0]
    one = state.start_session(first["id"], original["id"])
    clock[0] = "2026-09-01T10:02:00+00:00"
    state.finish_session(one["id"])
    second = replan(state, space_id, first)
    copied = second["tasks"][0]
    assert copied["id"] != original["id"]
    assert copied["node_id"] == original["node_id"]
    assert copied["sequence"] == original["sequence"]
    two = state.start_session(second["id"], copied["id"])
    clock[0] = "2026-09-01T10:05:00+00:00"
    state.finish_session(two["id"])
    record = factory().get_learning_record(second["id"], copied["id"])
    assert {row["session_id"] for row in record["sessions"]} == {one["id"], two["id"]}
    assert record["total_elapsed_seconds"] == 300
    assert record["topics"][0]["name"] == topic["name"]
    assert record["topics"][0]["source"] == "session_snapshot"
    assert record["topics"][0]["source_refs"] == topic["source_refs"]
    progress = state.get_progress(space_id)
    assert progress["total_count"] == 1
    assert len(progress["rounds"]) == 2
    assert progress["rounds"][0]["nodes"] == []
    node = progress["rounds"][1]["nodes"][0]
    assert node["plan_id"] == second["id"]
    assert node["task_id"] == copied["id"]
    assert node["node_id"] == original["node_id"]


def test_new_review_cycle_preserves_old_history_but_has_separate_record(workbench):
    _, state, space_id, topic, clock = workbench
    first = state.create_plan(space_id, {})
    original = first["tasks"][0]
    session = state.start_session(first["id"], original["id"])
    state.finish_session(session["id"])
    state.update_task(first["id"], original["id"], {"status": "completed", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="mastered", mastery_score=1.0,
               next_review_at="2026-09-02T10:00:00+00:00")
    clock[0] = "2026-09-03T10:00:00+00:00"
    second = replan(state, space_id, first)
    current = next(t for t in second["tasks"] if not t["context"].get("historical"))
    historical = next(t for t in second["tasks"] if t["context"].get("historical"))
    assert current["node_id"] != original["node_id"]
    assert historical["node_id"] == original["node_id"]
    assert state.get_learning_record(second["id"], current["id"])["sessions"] == []
    assert state.get_learning_record(second["id"], historical["id"])["sessions"][0]["session_id"] == session["id"]
    progress = state.get_progress(space_id)
    assert (progress["completed_count"], progress["total_count"]) == (1, 2)
    old_node = progress["rounds"][1]["nodes"][0]
    assert old_node["historical"] is True
    assert old_node["task_id"] == historical["id"]
    assert state.get_learning_record(second["id"], historical["id"])["current"] is False


def test_elapsed_deferral_preserves_same_node(workbench):
    _, state, space_id, _, clock = workbench
    first = state.create_plan(space_id, {})
    original = first["tasks"][0]
    state.update_task(first["id"], original["id"], {"status": "deferred", "expected_plan_version": 1,
                      "defer_until": "2026-09-02T10:00:00+00:00"})
    clock[0] = "2026-09-02T11:00:00+00:00"
    second = replan(state, space_id, first)
    assert second["tasks"][0]["node_id"] == original["node_id"]
    assert state.get_progress(space_id)["total_count"] == 1


def test_progress_paginates_rounds_descending_with_query_bound_cursor(workbench):
    _, state, space_id, _, clock = workbench
    plans = []
    for day in range(1, 5):
        clock[0] = f"2026-09-{day:02d}T10:00:00+00:00"
        plans.append(state.create_plan(space_id, {}))
    page = state.get_progress(space_id, limit=2)
    assert [r["plan_id"] for r in page["rounds"]] == [p["id"] for p in reversed(plans[2:])]
    assert page["next_cursor"]
    last = state.get_progress(space_id, limit=2, cursor=page["next_cursor"])
    assert [r["plan_id"] for r in last["rounds"]] == [p["id"] for p in reversed(plans[:2])]
    assert last["next_cursor"] is None
    assert last["total_count"] == page["total_count"] == 4
    other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
    with pytest.raises(DomainConflict, match="游标"):
        state.get_progress(other["id"], cursor=page["next_cursor"])
    for invalid in (0, 51, True):
        with pytest.raises(DomainConflict):
            state.get_progress(space_id, limit=invalid)


def test_linked_assessments_use_safe_result_and_ignore_unlinked_legacy(workbench):
    factory, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    session = state.start_session(plan["id"], task["id"])
    linked = assessment(state, space_id, learning_session_id=session["id"])
    unlinked = assessment(state, space_id)
    public = state.get_assessment(linked["id"])
    assert public["plan_id"] == plan["id"]
    assert public["task_id"] == task["id"]
    before = factory().get_learning_record(plan["id"], task["id"])
    assert [a["assessment_id"] for a in before["assessments"]] == [linked["id"]]
    assert before["assessments"][0]["result"] is None
    assert not any(key in json.dumps(before) for key in ("answer_key", "rubric", "source_text"))
    state.record_attempt(linked["id"], {"answers": [{"question_id": q["id"], "expected_answer_revision": 0,
                         "answer": "A"} for q in public["questions"]], "finalize": True})
    after = factory().get_learning_record(plan["id"], task["id"])
    assert after["assessments"][0]["result"] == state.assessment_result(linked["id"])
    assert unlinked["id"] not in {a["assessment_id"] for a in after["assessments"]}


def test_assessment_link_validates_exact_task_and_cross_space_without_creating_runs(workbench):
    _, state, space_id, _, _ = workbench
    first = state.create_plan(space_id, {})
    task = first["tasks"][0]
    session = state.start_session(first["id"], task["id"])
    state.finish_session(session["id"])
    second = replan(state, space_id, first)
    other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
    with pytest.raises(DomainConflict) as exc:
        assessment(state, other["id"], plan_id=second["id"], task_id=second["tasks"][0]["id"])
    assert exc.value.code == "PLAN_SPACE_MISMATCH"
    with pytest.raises(DomainConflict) as exc:
        assessment(state, space_id, plan_id=second["id"], task_id=second["tasks"][0]["id"], learning_session_id=session["id"])
    assert exc.value.code == "SESSION_TASK_MISMATCH"
    with pytest.raises(DomainConflict) as exc:
        assessment(state, space_id, plan_id=first["id"], task_id=task["id"])
    assert exc.value.code == "PLAN_NOT_ACTIVE"
    assert state.assessment_service.repository.records("assessments", space_id=space_id) == []


def test_completed_current_task_can_retest_but_historical_copy_cannot(workbench):
    _, state, space_id, topic, clock = workbench
    first = state.create_plan(space_id, {})
    task = first["tasks"][0]
    state.update_task(first["id"], task["id"], {"status": "completed", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="learning", mastery_score=0.7)
    assert state.get_plan(first["id"])["status"] == "needs_replan"
    assert assessment(state, space_id, plan_id=first["id"], task_id=task["id"])["id"]
    save_state(state, space_id, topic, status="mastered", mastery_score=1.0,
               next_review_at="2026-09-02T10:00:00+00:00")
    clock[0] = "2026-09-03T10:00:00+00:00"
    second = replan(state, space_id, first)
    historical = next(t for t in second["tasks"] if t["context"].get("historical"))
    with pytest.raises(DomainConflict) as exc:
        assessment(state, space_id, plan_id=second["id"], task_id=historical["id"])
    assert exc.value.code == "TASK_NOT_ACTIVE"


def test_frozen_topics_survive_current_source_change_and_legacy_fallback_is_labeled(workbench, monkeypatch):
    _, state, space_id, topic, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    session = state.start_session(plan["id"], task["id"])
    state.finish_session(session["id"])
    changed = {**topic, "name": "Updated heading", "source_refs": []}
    monkeypatch.setattr(state.space_service, "bound_topics", lambda _: [changed])
    record = state.get_learning_record(plan["id"], task["id"])
    assert record["topics"][0]["name"] == topic["name"]
    assert record["topics"][0]["source_refs"] == topic["source_refs"]
    assert record["topics"][0]["fallback"] is False
    assert state.get_progress(space_id)["rounds"][0]["nodes"][0]["title"] == topic["name"]
    legacy = {**task, "context": {"position": 0}}
    projected = state.plan_sessions._record_topics(state.space_service.repository.get(space_id), legacy, [])
    assert projected[0]["source"] == "current"
    assert projected[0]["fallback"] is True
    assert projected[0]["name"] == "Updated heading"


def test_linked_assessment_replay_and_generator_inputs_keep_association_out_of_model(workbench):
    factory, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    observed = []
    class Questions(FixedQuestions):
        def generate(self, topics, payload):
            observed.append(dict(payload))
            return super().generate(topics, payload)
    state.assessment_service.generator = Questions()
    payload = {"kind": "retest", "plan_id": plan["id"], "task_id": task["id"], "question_count": 5,
               "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}
    first = state.create_assessment(space_id, payload, idempotency_key="linked")
    assert factory().create_assessment(space_id, payload, idempotency_key="linked") == first
    assert len(observed) == 1
    assert not {"plan_id", "task_id", "learning_session_id"}.intersection(observed[0])


def test_read_projections_do_not_mutate_persisted_plan_status_or_context(workbench):
    _, state, space_id, topic, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    repository = state.plan_sessions.repository
    before = repository.get_record("plans", plan["id"], lock=False)
    save_state(state, space_id, topic, status="learning", mastery_score=0.7)
    assert state.get_workbench(space_id)["current_plan"]["status"] == "needs_replan"
    state.get_progress(space_id)
    state.get_learning_record(plan["id"], task["id"])
    assert repository.get_record("plans", plan["id"], lock=False) == before


def test_terminal_choices_count_only_completed_unique_nodes(workbench):
    _, state, space_id, _, clock = workbench
    plans = []
    for day, status in enumerate(("completed", "skipped", "deferred"), start=1):
        clock[0] = f"2026-09-{day:02d}T10:00:00+00:00"
        plan = state.create_plan(space_id, {})
        state.update_task(plan["id"], plan["tasks"][0]["id"], {"status": status, "expected_plan_version": 1,
                          "reason": "Choice", **({"defer_until": "2026-10-01T10:00:00+00:00"} if status == "deferred" else {})})
        plans.append(plan)
    progress = state.get_progress(space_id)
    assert (progress["completed_count"], progress["total_count"]) == (1, 3)


def test_learning_record_rejects_a_task_owned_by_another_plan(workbench):
    from knowpath_backend.learning.errors import DomainNotFound
    _, state, space_id, _, clock = workbench
    first = state.create_plan(space_id, {})
    clock[0] = "2026-09-02T10:00:00+00:00"
    second = state.create_plan(space_id, {})
    with pytest.raises(DomainNotFound):
        state.get_learning_record(second["id"], first["tasks"][0]["id"])


def test_legacy_identity_merges_proven_copy_but_not_new_cycle():
    from knowpath_backend.learning.plans.workbench import WorkbenchReads
    original = {"id": "old", "node_id": "old", "sequence": 1, "kind": "learn",
                "context": {"cycle_fingerprint": "cycle-one", "position": 0}}
    copied = {"id": "copy", "node_id": "copy", "sequence": 1, "kind": "learn",
              "context": {"origin_task_id": "old", "cycle_fingerprint": "cycle-one", "position": 0}}
    new = {"id": "new", "node_id": "new", "sequence": 1, "kind": "learn",
           "context": {"origin_task_id": "copy", "cycle_fingerprint": "cycle-two", "position": 0}}
    historical = {"id": "history", "node_id": "history", "sequence": 2, "kind": "learn",
                  "context": {"origin_task_id": "copy", "historical": True, "position": 1}}
    plans = [{"id": "first", "created_at": "2026-09-01T10:00:00+00:00", "tasks": [original]},
             {"id": "second", "created_at": "2026-09-02T10:00:00+00:00", "tasks": [copied]},
             {"id": "third", "created_at": "2026-09-03T10:00:00+00:00", "tasks": [new, historical]}]
    WorkbenchReads._resolve_legacy_nodes(plans)
    assert copied["node_id"] == historical["node_id"] == original["node_id"]
    assert new["node_id"] != original["node_id"]
    assert historical["sequence"] == original["sequence"]


def test_linked_assessment_history_survives_replan_and_does_not_merge_new_cycle(workbench):
    factory, state, space_id, topic, clock = workbench
    first = state.create_plan(space_id, {})
    task = first["tasks"][0]
    linked = assessment(state, space_id, plan_id=first["id"], task_id=task["id"])
    clock[0] = "2026-09-02T10:00:00+00:00"
    second = replan(state, space_id, first)
    copied = second["tasks"][0]
    assert factory().get_learning_record(second["id"], copied["id"])["assessments"][0]["assessment_id"] == linked["id"]
    state.update_task(second["id"], copied["id"], {"status": "completed", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="unstable", mastery_score=0.2, error_tags=["incorrect_answer"])
    clock[0] = "2026-09-03T10:00:00+00:00"
    third = replan(state, space_id, second)
    new = next(row for row in third["tasks"] if not row["context"].get("historical"))
    assert factory().get_learning_record(third["id"], new["id"])["assessments"] == []


def test_assessment_rejects_cross_space_session_and_non_task_topics(workbench):
    _, state, space_id, _, _ = workbench
    first = state.create_plan(space_id, {})
    task = first["tasks"][0]
    session = state.start_session(first["id"], task["id"])
    other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
    with pytest.raises(DomainConflict) as exc:
        assessment(state, other["id"], learning_session_id=session["id"])
    assert exc.value.code == "SESSION_SPACE_MISMATCH"
    with pytest.raises(DomainConflict) as exc:
        assessment(state, space_id, plan_id=first["id"], task_id=task["id"], topic_ids=["other-topic"])
    assert exc.value.code == "TOPIC_TASK_MISMATCH"


def test_page_cursor_stays_valid_after_new_round_is_inserted(workbench):
    _, state, space_id, _, clock = workbench
    plans = []
    for day in range(1, 4):
        clock[0] = f"2026-09-{day:02d}T10:00:00+00:00"
        plans.append(state.create_plan(space_id, {}))
    first_page = state.get_progress(space_id, limit=1)
    clock[0] = "2026-09-04T10:00:00+00:00"
    state.create_plan(space_id, {})
    remaining = state.get_progress(space_id, limit=2, cursor=first_page["next_cursor"])
    assert [row["plan_id"] for row in remaining["rounds"]] == [plan["id"] for plan in reversed(plans[:2])]
    assert remaining["next_cursor"] is None


def test_http_workbench_contract_and_optional_assessment_links():
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.test.material_upload_helpers import ParsedUploadClient
    with ParsedUploadClient(create_app(question_generator=FixedQuestions())) as client:
        upload = client.post("/api/v1/materials", files={"file": ("notes.md", b"# Functions\n\nReusable behavior.", "text/markdown")}, headers={"Idempotency-Key": "material"}).json()
        space = client.post("/api/v1/learning-spaces", json={"name": "Python", "material_ids": [upload["material"]["id"]]}, headers={"Idempotency-Key": "space"}).json()
        space_id = space["id"]
        assert client.get(f"/api/v1/learning-spaces/{space_id}/workbench").json()["current_plan"] is None
        created_response = client.post(f"/api/v1/learning-spaces/{space_id}/plans", json={}, headers={"Idempotency-Key": "plan"})
        assert created_response.status_code == 202, created_response.text
        created = created_response.json()
        plan = client.get(f"/api/v1/plans/{created['plan_id']}").json()
        task_id = plan["tasks"][0]["id"]
        response = client.post(f"/api/v1/learning-spaces/{space_id}/assessments", json={"plan_id": plan["id"], "task_id": task_id}, headers={"Idempotency-Key": "assessment"})
        assert response.status_code == 202
        assert client.get(f"/api/v1/learning-spaces/{space_id}/progress").json()["total_count"] == len(plan["tasks"])
        record = client.get(f"/api/v1/plans/{plan['id']}/tasks/{task_id}/learning-record")
        assert record.status_code == 200
        assert record.json()["assessments"][0]["assessment_id"] == response.json()["assessment_id"]
        assert client.get(f"/api/v1/learning-spaces/{space_id}/progress", params={"limit": 51}).status_code == 422
        assert client.get(f"/api/v1/learning-spaces/{space_id}/progress", params={"cursor": "invalid"}).status_code == 422


def test_superseded_task_write_cannot_change_current_node_or_version(workbench):
    factory, state, space_id, _, _ = workbench
    first = state.create_plan(space_id, {})
    original = first["tasks"][0]
    second = replan(state, space_id, first)
    before = state.get_progress(space_id)
    saved = state.get_plan(first["id"])
    with pytest.raises(DomainConflict) as exc:
        state.update_task(first["id"], original["id"], {"status": "completed",
            "note": "rewrite history", "expected_plan_version": saved["version"]})
    assert exc.value.code == "PLAN_NOT_ACTIVE"
    assert factory().get_plan(first["id"]) == saved
    assert factory().get_progress(space_id) == before
    assert factory().get_plan(second["id"])["tasks"][0]["status"] == "pending"


def test_historical_copy_is_immutable_but_current_completed_notes_can_be_saved(workbench):
    factory, state, space_id, topic, clock = workbench
    first = state.create_plan(space_id, {})
    original = first["tasks"][0]
    state.update_task(first["id"], original["id"], {"status": "completed", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="mastered", mastery_score=1.0,
               next_review_at="2026-09-02T10:00:00+00:00")
    clock[0] = "2026-09-03T10:00:00+00:00"
    second = replan(state, space_id, first)
    historical = next(t for t in second["tasks"] if t["context"].get("historical"))
    before = state.get_plan(second["id"])
    with pytest.raises(DomainConflict) as exc:
        state.update_task(second["id"], historical["id"], {"status": "completed",
            "note": "rewrite history", "expected_plan_version": second["version"]})
    assert exc.value.code == "TASK_NOT_ACTIVE"
    assert factory().get_plan(second["id"]) == before
    current = next(t for t in second["tasks"] if not t["context"].get("historical"))
    state.update_task(second["id"], current["id"], {"status": "completed", "expected_plan_version": before["version"]})
    saved = state.get_plan(second["id"])
    state.update_task(second["id"], current["id"], {"status": "completed", "note": "current notes",
        "expected_plan_version": saved["version"]})
    assert factory().get_plan(second["id"])["version"] == saved["version"] + 1
    assert factory().get_learning_record(second["id"], current["id"])["task"]["note"] == "current notes"


def test_preference_replan_preserves_same_cycle_node_and_session(workbench):
    factory, state, space_id, _, _ = workbench
    first = state.create_plan(space_id, {})
    task = first['tasks'][0]
    learning = state.start_session(first['id'], task['id'])
    state.finish_session(learning['id'])
    second = state.create_plan(space_id, {'rebuild_mode': 'local_replan',
        'base_plan_id': first['id'], 'expected_plan_version': first['version'],
        'include_review': False, 'minutes_per_session': 45})
    replacement = second['tasks'][0]
    assert replacement['context']['cycle_fingerprint'] == task['context']['cycle_fingerprint']
    assert replacement['node_id'] == task['node_id']
    assert replacement['status'] == 'pending'
    assert second['config']['minutes_per_session'] == 45
    assert factory().get_progress(space_id)['total_count'] == 1
    assert factory().get_learning_record(second['id'], replacement['id'])['sessions'][0]['session_id'] == learning['id']


def test_new_initial_plan_closes_previous_writes_sessions_and_associations(workbench):
    factory, state, space_id, _, clock = workbench
    first = state.create_plan(space_id, {}, idempotency_key='first-initial')
    task = first['tasks'][0]
    learning = state.start_session(first['id'], task['id'])
    clock[0] = '2026-09-01T10:01:00+00:00'
    second = state.create_plan(space_id, {}, idempotency_key='second-initial')
    assert factory().get_workbench(space_id)['current_plan']['id'] == second['id']
    saved = state.get_plan(first['id'])
    assert saved['status'] == 'superseded'
    with pytest.raises(DomainConflict) as exc:
        state.update_task(first['id'], task['id'], {'status': 'completed', 'expected_plan_version': saved['version']})
    assert exc.value.code == 'PLAN_NOT_ACTIVE'
    state.finish_session(learning['id'])
    with pytest.raises(DomainConflict) as exc:
        state.start_session(first['id'], task['id'])
    assert exc.value.code == 'PLAN_NOT_ACTIVE'
    with pytest.raises(DomainConflict) as exc:
        assessment(state, space_id, plan_id=first['id'], task_id=task['id'])
    assert exc.value.code == 'PLAN_NOT_ACTIVE'
    assert factory().get_learning_record(first['id'], task['id'])['sessions'][0]['status'] == 'finished'


def test_legacy_initial_plan_is_read_only_when_newer_plan_exists(workbench):
    _, state, space_id, _, clock = workbench
    first = state.create_plan(space_id, {})
    clock[0] = '2026-09-01T10:01:00+00:00'
    second = state.create_plan(space_id, {})
    stored = state.plan_sessions.repository.get_record('plans', first['id'], lock=False)
    stored['status'] = 'ready'
    state.plan_sessions.repository.put_record('plans', stored)
    assert state.get_workbench(space_id)['current_plan']['id'] == second['id']
    assert state.get_plan(first['id'])['status'] == 'superseded'
    with pytest.raises(DomainConflict) as exc:
        state.update_task(first['id'], first['tasks'][0]['id'], {'status': 'completed', 'expected_plan_version': stored['version']})
    assert exc.value.code == 'PLAN_NOT_ACTIVE'


def test_missing_legacy_session_timestamp_keeps_duration_unknown(workbench):
    _, state, space_id, _, _ = workbench
    if state.plan_sessions.sql:
        pytest.skip('SQL sessions enforce non-null start timestamps')
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    learning = state.start_session(plan['id'], task['id'])
    state.finish_session(learning['id'])
    row = state.plan_sessions.repository.get_record('sessions', learning['id'])
    row['started_at'] = None
    row['context'].pop('finish_elapsed_seconds', None)
    row.pop('elapsed_seconds', None)
    state.plan_sessions.repository.put_record('sessions', row)
    record = state.get_learning_record(plan['id'], task['id'])
    assert record['sessions'][0]['elapsed_seconds'] is None
    assert record['total_elapsed_seconds'] is None


def test_same_timestamp_inheritance_uses_successor_not_uuid_for_progress(workbench, monkeypatch):
    import knowpath_backend.learning.plans.service as planner
    factory, state, space_id, _, _ = workbench
    ids = iter(['ffffffff-ffff-4fff-8fff-ffffffffffff',
                '77777777-7777-4777-8777-777777777777',
                '11111111-1111-4111-8111-111111111111'])
    monkeypatch.setattr(planner, 'uid', lambda: next(ids))
    first = state.create_plan(space_id, {})
    second = replan(state, space_id, first)
    third = replan(state, space_id, second)
    task = third['tasks'][0]
    state.update_task(third['id'], task['id'],
        {'status': 'completed', 'expected_plan_version': third['version']})
    progress = factory().get_progress(space_id)
    assert progress['completed_count'] == progress['total_count'] == 1
    origin = next(round for round in progress['rounds'] if round['plan_id'] == first['id'])
    assert origin['nodes'][0]['plan_id'] == third['id']
    assert origin['nodes'][0]['task_id'] == task['id']
    assert origin['nodes'][0]['status'] == 'completed'
    assert all(not round['nodes'] for round in progress['rounds'] if round['plan_id'] != first['id'])


def test_legacy_nodes_resolve_origin_before_same_timestamp_uuid_order():
    from knowpath_backend.learning.plans.workbench import WorkbenchReads
    first = {'id': 'root', 'node_id': 'root', 'sequence': 1, 'kind': 'learn',
             'context': {'cycle_fingerprint': 'same'}}
    second = {'id': 'copy', 'node_id': 'copy', 'sequence': 1, 'kind': 'learn',
              'context': {'cycle_fingerprint': 'same', 'origin_task_id': 'root'}}
    third = {'id': 'last', 'node_id': 'last', 'sequence': 1, 'kind': 'learn',
             'context': {'cycle_fingerprint': 'same', 'origin_task_id': 'copy'}}
    plans = [{'id': identifier, 'created_at': '2026-09-01T10:00:00+00:00', 'tasks': [task]}
             for identifier, task in [('z', first), ('m', second), ('a', third)]]
    WorkbenchReads._resolve_legacy_nodes(plans)
    assert second['node_id'] == third['node_id'] == first['node_id']


def test_transaction_plan_list_refreshes_stale_plan_and_task(workbench):
    from sqlalchemy import update
    from knowpath_backend.learning.persistence.db import StudyPlanRow, StudyTaskRow
    _, state, space_id, _, _ = workbench
    service = state.plan_sessions
    if not service.sql:
        pytest.skip('SQL identity-map regression')
    plan = state.create_plan(space_id, {})
    with service._tx():
        session = service.uow._current.get()
        old_plan, old_tasks = service._get_plan_sql(plan['id'])
        session.execute(update(StudyPlanRow).where(StudyPlanRow.id == plan['id'])
            .values(status='superseded').execution_options(synchronize_session=False))
        session.execute(update(StudyTaskRow).where(StudyTaskRow.id == old_tasks[0].id)
            .values(status='completed').execution_options(synchronize_session=False))
        assert old_plan.status == 'ready'
        assert old_tasks[0].status == 'pending'
        refreshed = service._space_plans(space_id)[0]
        assert refreshed['status'] == 'superseded'
        assert refreshed['tasks'][0]['status'] == 'completed'
        assert service._current_plan_id(space_id) is None


def test_transaction_plan_lists_use_mysql_current_reads(workbench):
    from sqlalchemy import event
    from sqlalchemy.dialects import mysql
    _, state, space_id, _, _ = workbench
    service = state.plan_sessions
    if not service.sql:
        pytest.skip('SQL locking-read regression')
    state.create_plan(space_id, {})
    statements = []
    def capture(connection, statement, multiparams, params, options):
        text = str(statement.compile(dialect=mysql.dialect()))
        if text.startswith('SELECT') and ('FROM study_plans' in text or 'FROM study_tasks' in text):
            statements.append((text, options))
    event.listen(service.uow.engine, 'before_execute', capture)
    try:
        with service._tx():
            service.spaces.repository.get(space_id)
            service._space_plans(space_id)
    finally:
        event.remove(service.uow.engine, 'before_execute', capture)
    assert len(statements) == 2
    assert all('FOR UPDATE' in text and options.get('populate_existing') for text, options in statements)
