"""A historical completion must not suppress a later executable learning cycle."""
from uuid import uuid4

import pytest

from knowpath_backend.learning.assessments.mastery import aggregate
from knowpath_backend.learning.assessments.service import revision
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_plans_sessions import backend, seed


@pytest.fixture
def cycle_workspace(backend, monkeypatch):
    import knowpath_backend.learning.plans.service as planner
    clock = ["2026-09-01T10:00:00+00:00"]
    monkeypatch.setattr(planner, "now", lambda: clock[0])
    state, space_id, topics = seed(backend)
    topic = topics[0]
    state.set_scope(space_id, {"topic_ids": [topic["id"]], "expected_version": 2})
    return state, space_id, topic, clock


def save_state(state, space_id, topic, **changes):
    repository = state.assessment_service.repository
    rows = repository.records("states", space_id=space_id, topic_id=topic["id"])
    value = aggregate(topic["id"], revision(topic), [], {}, 1, "2026-08-31T10:00:00+00:00")
    value.update(id=rows[0]["id"] if rows else str(uuid4()), space_id=space_id, epoch=0, **changes)
    repository.put_record("states", value)


def replan(state, space_id, plan):
    current = state.get_plan(plan["id"])
    return state.create_plan(space_id, {"rebuild_mode": "local_replan",
        "base_plan_id": plan["id"], "expected_plan_version": current["version"]})


def executable(plan):
    return [t for t in plan["tasks"] if not t["context"].get("historical")
            and t["status"] in {"pending", "in_progress"}]


@pytest.mark.parametrize("terminal", ["completed", "skipped"])
def test_new_due_review_preserves_terminal_learning_as_history(cycle_workspace, terminal):
    state, space_id, topic, clock = cycle_workspace
    plan = state.create_plan(space_id, {})
    original = plan["tasks"][0]
    state.update_task(plan["id"], original["id"],
        {"status": terminal, "reason": "Recorded choice", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="mastered", mastery_score=1.0,
               next_review_at="2026-09-08T10:00:00+00:00")
    clock[0] = "2026-09-09T10:00:00+00:00"
    rebuilt = replan(state, space_id, plan)
    assert [t["kind"] for t in executable(rebuilt)] == ["review"]
    history = [t for t in rebuilt["tasks"] if t["context"].get("historical")]
    assert len(history) == 1
    assert history[0]["status"] == terminal
    assert history[0]["context"]["origin_task_id"] == original["id"]
    assert LearningState(state.material_service.repository).get_plan(rebuilt["id"])["tasks"] == rebuilt["tasks"]


def test_new_weak_evidence_creates_practice_after_completed_learning(cycle_workspace):
    state, space_id, topic, _ = cycle_workspace
    plan = state.create_plan(space_id, {})
    state.update_task(plan["id"], plan["tasks"][0]["id"],
        {"status": "completed", "expected_plan_version": 1})
    save_state(state, space_id, topic, status="unstable", mastery_score=0.2, error_tags=["incorrect_answer"])
    rebuilt = replan(state, space_id, plan)
    assert [t["kind"] for t in executable(rebuilt)] == ["targeted_practice"]


def test_completed_diagnostic_does_not_repeat_for_unchanged_stale_state(cycle_workspace):
    state, space_id, topic, _ = cycle_workspace
    save_state(state, space_id, topic, score_validity="stale", status="needs_review")
    plan = state.create_plan(space_id, {})
    assert plan["tasks"][0]["kind"] == "diagnostic"
    state.update_task(plan["id"], plan["tasks"][0]["id"],
        {"status": "completed", "expected_plan_version": 1})
    rebuilt = replan(state, space_id, plan)
    assert executable(rebuilt) == []
    assert len(rebuilt["tasks"]) == 1


@pytest.mark.parametrize("terminal", ["completed", "skipped"])
def test_same_review_cycle_does_not_repeat_without_new_evidence(cycle_workspace, terminal):
    state, space_id, topic, clock = cycle_workspace
    save_state(state, space_id, topic, status="mastered", next_review_at="2026-08-31T10:00:00+00:00")
    plan = state.create_plan(space_id, {})
    assert plan["tasks"][0]["kind"] == "review"
    state.update_task(plan["id"], plan["tasks"][0]["id"],
        {"status": terminal, "reason": "Already handled", "expected_plan_version": 1})
    clock[0] = "2026-09-02T10:00:00+00:00"
    rebuilt = replan(state, space_id, plan)
    assert executable(rebuilt) == []
    assert any(t["status"] == terminal for t in rebuilt["tasks"])
    save_state(state, space_id, topic, status="mastered", state_version=2, next_review_at="2026-09-03T10:00:00+00:00")
    clock[0] = "2026-09-04T10:00:00+00:00"
    next_cycle = replan(state, space_id, rebuilt)
    assert [t["kind"] for t in executable(next_cycle)] == ["review"]
    again = replan(state, space_id, next_cycle)
    assert len(executable(again)) == 1
    assert len([t for t in again["tasks"] if t["context"].get("historical")]) == 1


def test_elapsed_deferral_restores_pending_task_and_preserves_trace(cycle_workspace):
    state, space_id, _, clock = cycle_workspace
    plan = state.create_plan(space_id, {})
    original = plan["tasks"][0]
    state.update_task(plan["id"], original["id"], {"status": "deferred",
        "expected_plan_version": 1, "defer_until": "2026-09-02T12:00:00+00:00", "note": "Tomorrow"})
    clock[0] = "2026-09-02T11:59:59+00:00"
    before = replan(state, space_id, plan)
    assert before["tasks"][0]["status"] == "deferred"
    clock[0] = "2026-09-02T12:00:00+00:00"
    after = replan(state, space_id, before)
    task = executable(after)[0]
    assert task["status"] == "pending"
    assert task["defer_until"] is None
    assert task["note"] == "Tomorrow"
    assert task["context"]["origin_task_id"] == before["tasks"][0]["id"]
    assert state.get_plan(after["id"])["status"] == "ready"
    assert state.start_session(after["id"], task["id"])["status"] == "active"


def test_future_deferral_survives_updated_state(cycle_workspace):
    state, space_id, topic, _ = cycle_workspace
    plan = state.create_plan(space_id, {})
    state.update_task(plan["id"], plan["tasks"][0]["id"], {"status": "deferred",
        "expected_plan_version": 1, "defer_until": "2026-09-04T10:00:00+00:00"})
    save_state(state, space_id, topic, status="unstable", mastery_score=0.1)
    rebuilt = replan(state, space_id, plan)
    assert executable(rebuilt) == []
    assert any(t["status"] == "deferred" and t["defer_until"] == "2026-09-04T10:00:00+00:00"
               for t in rebuilt["tasks"])


def test_future_deferred_task_cannot_start_early(cycle_workspace):
    state, space_id, _, _ = cycle_workspace
    plan = state.create_plan(space_id, {})
    task = plan["tasks"][0]
    state.update_task(plan["id"], task["id"], {"status": "deferred",
        "expected_plan_version": 1, "defer_until": "2026-09-04T10:00:00+00:00"})
    with pytest.raises(DomainConflict) as error:
        state.start_session(plan["id"], task["id"])
    assert error.value.code == "TASK_NOT_ACTIVE"


def test_clock_only_review_transition_marks_plan_for_replanning(cycle_workspace):
    state, space_id, topic, clock = cycle_workspace
    save_state(state, space_id, topic, status="mastered", next_review_at="2026-09-08T10:00:00+00:00")
    plan = state.create_plan(space_id, {})
    assert executable(plan) == []
    clock[0] = "2026-09-02T10:00:00+00:00"
    assert state.get_plan(plan["id"])["status"] == "ready"
    clock[0] = "2026-09-09T10:00:00+00:00"
    assert state.get_plan(plan["id"])["status"] == "needs_replan"
    rebuilt = replan(state, space_id, plan)
    assert [t["kind"] for t in executable(rebuilt)] == ["review"]
    assert state.get_plan(rebuilt["id"])["status"] == "ready"


def test_clock_only_deferral_transition_marks_plan_for_replanning(cycle_workspace):
    state, space_id, _, clock = cycle_workspace
    plan = state.create_plan(space_id, {})
    state.update_task(plan["id"], plan["tasks"][0]["id"], {"status": "deferred",
        "expected_plan_version": 1, "defer_until": "2026-09-02T12:00:00+00:00"})
    assert state.get_plan(plan["id"])["status"] == "ready"
    clock[0] = "2026-09-02T12:00:00+00:00"
    assert state.get_plan(plan["id"])["status"] == "needs_replan"


@pytest.mark.parametrize("kind,status", [("learn", "learning"), ("review", "mastered")])
@pytest.mark.parametrize("original_include_review", [True, False])
def test_legacy_terminal_task_keeps_its_completed_cycle(kind, status, original_include_review):
    from knowpath_backend.learning.plans.policy import PlannerPolicy, build_tasks, topic_fingerprint
    topic = {"id": "topic", "source_refs": [], "prerequisites": []}
    old_state = {"status": status, "next_review_at": "2026-08-31T10:00:00+00:00"}
    config = {"include_review": True, "session_count": 3, "minutes_per_session": 30}
    previous = {"id": "legacy", "topic_ids": ["topic"], "kind": kind,
        "status": "completed", "estimated_minutes": 10, "reason": "Already handled",
        "defer_until": None, "note": None, "context": {"policy_version": "planner-v1",
            "input_fingerprint": topic_fingerprint(topic, old_state,
                {**config, "include_review": original_include_review}, PlannerPolicy(version="planner-v1"))}}
    tasks = build_tasks([topic], {"topic": {**old_state, "review_due": True}}, config, {},
        [previous], "2026-09-01T10:00:00+00:00", PlannerPolicy())
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"


@pytest.mark.parametrize("status,validity", [("needs_review", "stale"), ("learning", "valid"), ("unstable", "valid")])
def test_review_clock_does_not_repeat_completed_nonreview_task(cycle_workspace, status, validity):
    state, space_id, topic, clock = cycle_workspace
    save_state(state, space_id, topic, status=status, score_validity=validity,
               next_review_at="2026-09-02T10:00:00+00:00")
    plan = state.create_plan(space_id, {})
    assert plan["tasks"][0]["kind"] != "review"
    state.update_task(plan["id"], plan["tasks"][0]["id"],
        {"status": "completed", "expected_plan_version": 1})
    clock[0] = "2026-09-03T10:00:00+00:00"
    rebuilt = replan(state, space_id, plan)
    assert executable(rebuilt) == []
    assert len(rebuilt["tasks"]) == 1


def test_invalidated_task_retains_future_deferral_on_new_diagnostic():
    from knowpath_backend.learning.plans.policy import PlannerPolicy, build_tasks
    topic = {"id": "topic", "source_refs": [], "prerequisites": []}
    config = {"include_review": True, "session_count": 3, "minutes_per_session": 30}
    previous = {"id": "old", "topic_ids": ["topic"], "kind": "learn",
        "status": "deferred", "estimated_minutes": 10, "reason": "Learn",
        "defer_until": "2026-09-20T10:00:00+00:00", "note": "After vacation",
        "context": {"knowledge_invalidated": True}}
    states = {"topic": {"status": "needs_review", "score_validity": "stale"}}
    tasks = build_tasks([topic], states, config, {}, [previous], "2026-09-02T10:00:00+00:00", PlannerPolicy())
    current = [t for t in tasks if not t["context"].get("historical")]
    assert len(current) == 1
    assert current[0]["kind"] == "diagnostic"
    assert current[0]["status"] == "deferred"
    assert current[0]["defer_until"] == previous["defer_until"]
    assert current[0]["note"] == previous["note"]
    assert len([t for t in tasks if t["context"].get("historical")]) == 1
    next_tasks = build_tasks([topic], states, config, {}, tasks, "2026-09-20T10:00:00+00:00", PlannerPolicy())
    restored = [t for t in next_tasks if not t["context"].get("historical")][0]
    assert restored["status"] == "pending"
    assert restored["kind"] == "diagnostic"
