"""Behavioral regressions for plan scheduling and current database reads."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, update

from my_agent_llms.learning.db import init_db, SessionRow, StudyTaskRow
from my_agent_llms.learning.errors import DomainConflict
from my_agent_llms.learning.materials import InMemoryMaterialRepository
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository
from my_agent_llms.test.test_learning_plans_sessions import seed, backend


def test_historical_pending_task_cannot_start(backend):
    state, space_id, topics = seed(backend)
    plan = state.create_plan(space_id, {})
    removed = plan["tasks"][0]["topic_ids"][0]
    state.set_scope(space_id, {"topic_ids": [t["id"] for t in topics if t["id"] != removed], "expected_version": 2})
    new = state.create_plan(space_id, {"rebuild_mode": "local_replan", "base_plan_id": plan["id"], "expected_plan_version": 1})
    historical = next(t for t in new["tasks"] if t["context"].get("historical"))
    with pytest.raises(DomainConflict) as exc:
        state.start_session(new["id"], historical["id"])
    assert exc.value.code == "TASK_NOT_ACTIVE"
    active = next(t for t in new["tasks"] if not t["context"].get("historical"))
    assert state.start_session(new["id"], active["id"])["status"] == "active"


def test_deferred_prerequisite_rejects_impossible_replan(backend, monkeypatch):
    state, space_id, topics = seed(backend)
    # Inject a validated prerequisite, the same input supplied by published graph bindings.
    bound = state.space_service.bound_topics
    def with_dependency(space):
        result = bound(space)
        result[1]["prerequisites"] = [result[0]["id"]]
        return result
    monkeypatch.setattr(state.space_service, "bound_topics", with_dependency)
    plan = state.create_plan(space_id, {})
    first = next(t for t in plan["tasks"] if t["topic_ids"] == [topics[0]["id"]])
    state.update_task(plan["id"], first["id"], {"status": "deferred", "expected_plan_version": 1, "defer_until": "2099-01-01T00:00:00+00:00"})
    with pytest.raises(DomainConflict) as exc:
        state.create_plan(space_id, {"rebuild_mode": "local_replan", "base_plan_id": plan["id"], "expected_plan_version": 2})
    assert exc.value.code == "PLAN_CONSTRAINT_UNSATISFIABLE"
    assert "deferred_prerequisite" in exc.value.details["conflicts"]
    assert state.get_plan(plan["id"])["status"] == "ready"
    assert state.get_plan(plan["id"])["version"] == 2


def test_due_mastered_topic_precedes_weak_topic(monkeypatch):
    import my_agent_llms.learning.planner as planner
    monkeypatch.setattr(planner, "now", lambda: "2026-09-18T10:00:00+00:00")
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    for index, topic in enumerate(topics):
        repo.assessment_data["states"][topic["id"]] = {
            "id": topic["id"], "space_id": space_id, "topic_id": topic["id"],
            "status": "mastered" if index == 0 else "unstable",
            "next_review_at": "2026-09-17T00:00:00+00:00" if index == 0 else None}
    plan = state.create_plan(space_id, {})
    assert plan["tasks"][0]["topic_ids"] == [topics[0]["id"]]
    assert plan["tasks"][0]["kind"] == "review"


def test_practice_estimate_cannot_be_shortened_to_fit_slot():
    repo = InMemoryMaterialRepository()
    state, space_id, topics = seed(repo)
    topic = topics[0]
    state.set_scope(space_id, {"topic_ids": [topic["id"]], "expected_version": 2})
    repo.assessment_data["states"][topic["id"]] = {
        "id": topic["id"], "space_id": space_id, "topic_id": topic["id"], "status": "unstable"}
    with pytest.raises(DomainConflict) as exc:
        state.create_plan(space_id, {"minutes_per_session": 10})
    assert exc.value.code == "PLAN_CONSTRAINT_UNSATISFIABLE"
    assert "minutes_per_session" in exc.value.details["conflicts"]
    assert not repo.assessment_data["plans"]
    plan = state.create_plan(space_id, {"minutes_per_session": 15})
    practice = next(t for t in plan["tasks"] if t["topic_ids"] == [topic["id"]])
    assert practice["estimated_minutes"] == 15


def test_locked_reads_refresh_previously_loaded_rows(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'refresh.db'}")
    init_db(engine)
    state, space_id, _ = seed(SqlAlchemyMaterialRepository(engine))
    plan = state.create_plan(space_id, {})
    started = state.start_session(plan["id"], plan["tasks"][0]["id"])
    service = state.plan_sessions
    with service._tx():
        db = service.uow._current.get()
        old_session = service._find_session_sql(started["id"])
        _, old_tasks = service._get_plan_sql(plan["id"])
        ended = datetime(2026, 9, 18, 10)
        # Database-side writes deliberately leave the identity map stale.
        db.execute(update(SessionRow).where(SessionRow.id == started["id"]).values(status="finished", active_space_id=None, finished_at=ended).execution_options(synchronize_session=False))
        db.execute(update(StudyTaskRow).where(StudyTaskRow.id == old_tasks[0].id).values(status="completed").execution_options(synchronize_session=False))
        assert old_session.status == "active"
        assert old_tasks[0].status == "pending"
        refreshed = service._find_session_sql(started["id"], lock=True)
        assert refreshed.status == "finished"
        assert refreshed.finished_at == ended
        _, refreshed_tasks = service._get_plan_sql(plan["id"], lock=True)
        assert refreshed_tasks[0].status == "completed"
    engine.dispose()
