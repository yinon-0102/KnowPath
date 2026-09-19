"""Opt-in two-connection regressions; clean up only fixture-owned records."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import delete, select

from knowpath_backend.learning.db import StudyPlanRow, StudyTaskRow, SessionRow, SessionEventRow
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.test.test_learning_state_persistence import workspace, create, answer


@pytest.fixture
def plans_workspace(workspace):
    factory, space_id, topic_id, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("two-connection regression requires MySQL")
    try:
        yield workspace
    finally:
        with engine.begin() as connection:
            session_ids = select(SessionRow.id).where(SessionRow.space_id == space_id)
            plan_ids = select(StudyPlanRow.id).where(StudyPlanRow.space_id == space_id)
            connection.execute(delete(SessionEventRow).where(SessionEventRow.session_id.in_(session_ids)))
            connection.execute(delete(SessionRow).where(SessionRow.space_id == space_id))
            connection.execute(delete(StudyTaskRow).where(StudyTaskRow.plan_id.in_(plan_ids)))
            connection.execute(delete(StudyPlanRow).where(StudyPlanRow.space_id == space_id))


@pytest.mark.parametrize("operation", ["event", "finish"])
def test_session_waiter_observes_committed_finish(plans_workspace, monkeypatch, operation):
    factory, space_id, _, _ = plans_workspace
    writer, reader = factory(), factory()
    plan = writer.create_plan(space_id, {})
    started = writer.start_session(plan["id"], plan["tasks"][0]["id"])
    loaded, resume = Event(), Event()
    original = reader.space_service.repository.get
    def pause_before_space_lock(identifier):
        loaded.set()
        assert resume.wait(10), "reader was not released"
        return original(identifier)
    monkeypatch.setattr(reader.space_service.repository, "get", pause_before_space_lock)
    def waiting_command():
        if operation == "event":
            return reader.add_session_event(started["id"], {"type": "pause", "event_id": started["id"]})
        return reader.finish_session(started["id"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiting_command)
        try:
            assert loaded.wait(10)
            finished = writer.finish_session(started["id"])
            with writer.plan_sessions._tx():
                finished_at = writer.plan_sessions._find_session_sql(started["id"], lock=True).finished_at
        finally:
            resume.set()
        if operation == "event":
            with pytest.raises(DomainConflict) as exc:
                future.result(timeout=10)
            assert exc.value.code == "SESSION_FINISHED"
        else:
            assert future.result(timeout=10) == finished
        with writer.plan_sessions._tx():
            assert writer.plan_sessions._find_session_sql(started["id"], lock=True).finished_at == finished_at


def test_start_waiter_rejects_completed_task(plans_workspace, monkeypatch):
    factory, space_id, _, _ = plans_workspace
    writer, reader = factory(), factory()
    plan = writer.create_plan(space_id, {})
    loaded, resume = Event(), Event()
    original = reader.plan_sessions._get_plan_sql
    def pause_after_plan_read(identifier, lock=False):
        result = original(identifier, lock=lock)
        if not lock and not loaded.is_set():
            loaded.set()
            assert resume.wait(10)
        return result
    monkeypatch.setattr(reader.plan_sessions, "_get_plan_sql", pause_after_plan_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reader.start_session, plan["id"], plan["tasks"][0]["id"])
        try:
            assert loaded.wait(10)
            writer.update_task(plan["id"], plan["tasks"][0]["id"], {"status": "completed", "expected_plan_version": 1})
        finally:
            resume.set()
        with pytest.raises(DomainConflict) as exc:
            future.result(timeout=10)
        assert exc.value.code == "TASK_NOT_ACTIVE"
        assert writer.plan_sessions.active_session_id(space_id) is None


def test_plan_creation_reads_grading_committed_after_snapshot(plans_workspace, monkeypatch):
    factory, space_id, topic_id, _ = plans_workspace
    writer, reader = factory(), factory()
    assessment = create(writer, space_id)
    answer(writer, assessment)
    waiting = Event()
    original = reader.space_service.repository.get
    def before_space_lock(identifier):
        waiting.set()
        return original(identifier)
    monkeypatch.setattr(reader.space_service.repository, "get", before_space_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with writer.assessment_service.repository.transaction():
            writer.finalize_assessment(assessment["id"], {"allow_unanswered": False})
            # The idempotency lookup establishes an old RR snapshot before
            # waiting for the space lock owned by the grading transaction.
            future = pool.submit(reader.create_plan, space_id, {}, idempotency_key="concurrent-plan")
            assert waiting.wait(10)
        plan = future.result(timeout=10)
    assert topic_id in plan["config"]["snapshot"]["states"]
    assert plan["config"]["snapshot"]["states"][topic_id]["evidence_ids"]
    assert writer.get_plan(plan["id"])["status"] == "ready"


def test_finish_duration_survives_mysql_fractional_second_rounding(plans_workspace, monkeypatch):
    from datetime import datetime
    factory, space_id, _, _ = plans_workspace
    state = factory()
    plan = state.create_plan(space_id, {})
    started = state.start_session(plan["id"], plan["tasks"][0]["id"])
    with state.plan_sessions._tx():
        row = state.plan_sessions._find_session_sql(started["id"], lock=True)
        row.started_at = datetime(2026, 9, 18, 12, 0, 0)
    monkeypatch.setattr("knowpath_backend.learning.planner.now", lambda: "2026-09-18T12:00:00.900000+00:00")
    first = state.finish_session(started["id"])
    assert first["elapsed_seconds"] == 0
    assert factory().finish_session(started["id"]) == first
