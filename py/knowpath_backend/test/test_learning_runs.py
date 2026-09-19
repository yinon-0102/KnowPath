import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.errors import DomainConflict, EventHistoryExpired
from knowpath_backend.learning.runs import InMemoryRunRepository, RunService, stream_run_events
from knowpath_backend.learning.persistence.run_repository import SqlAlchemyRunRepository


@pytest.fixture(params=["memory", "sql"])
def runtime(request, tmp_path):
    now = [datetime(2026, 9, 18, tzinfo=timezone.utc)]
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'runs.db').as_posix()}")
        init_db(engine)
        repo = SqlAlchemyRunRepository(engine)
    else:
        repo = InMemoryRunRepository()
    yield RunService(repo, clock=lambda: now[0]), now, engine
    if engine is not None:
        engine.dispose()


def test_cancellation_blocks_late_result_and_preserves_terminal(runtime):
    service, _, _ = runtime
    run = service.create("demo", status="running")
    run_id = run["id"]
    service.request_cancel(run_id)
    before = service.get(run_id)
    with pytest.raises(DomainConflict, match="取消"):
        service.complete(run_id, {"type": "assessment", "id": "late"})
    assert service.get(run_id) == before
    service.acknowledge_cancel(run_id)
    cancelled = service.get(run_id)
    assert cancelled["result_ref"] is None
    assert service.request_cancel(run_id) == cancelled
    assert service.acknowledge_cancel(run_id) == cancelled
    assert [e["event"] for e in cancelled["events"]] == [
        "run.started", "run.cancelling", "run.cancelled"
    ]


def test_events_are_atomic_and_sequential(runtime):
    service, _, _ = runtime
    run_id = service.create("demo", status="running")["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: service.append_event(run_id, "progress", {"percent": i}), range(1, 13)))
    service.complete(run_id, {"type": "export", "id": "ready"})
    events = service.events_for(run_id)
    assert [int(e["id"]) for e in events] == list(range(1, 15))
    assert all(datetime.fromisoformat(e["data"]["timestamp"]).tzinfo for e in events)
    assert events[-1]["event"] == "run.completed"
    with pytest.raises(DomainConflict):
        service.append_event(run_id, "message.delta", {"delta": "too late"})


def test_expiry_applies_to_requested_history_and_preserves_final_status(runtime):
    service, now, _ = runtime
    run_id = service.create("demo", status="running")["id"]
    now[0] += timedelta(days=6)
    service.complete(run_id, {"type": "export", "id": "file"})
    now[0] += timedelta(days=2)
    with pytest.raises(EventHistoryExpired):
        service.events_for(run_id)
    assert [e["id"] for e in service.events_for(run_id, after_id=1)] == ["2"]
    assert service.get(run_id)["status"] == "succeeded"
    assert service.get(run_id)["events"][0]["data"] is None
    now[0] += timedelta(days=7)
    with pytest.raises(EventHistoryExpired):
        service.events_for(run_id, after_id=1)
    assert service.events_for(run_id, after_id=2) == []


def test_stream_waits_for_new_events_sends_heartbeat_and_closes(runtime):
    service, _, _ = runtime
    run_id = service.create("demo", status="running")["id"]

    async def consume():
        stream = stream_run_events(service, run_id, heartbeat_seconds=0.01, poll_seconds=0.005)
        try:
            assert "run.started" in await anext(stream)
            assert await asyncio.wait_for(anext(stream), 2) == ": heartbeat\n\n"
            service.append_event(run_id, "message.delta", {"delta": "你好"})
            assert "你好" in await asyncio.wait_for(anext(stream), 2)
            service.complete(run_id, {"type": "message", "id": "reply"})
            assert "run.completed" in await anext(stream)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(consume())


def test_stream_disconnect_stops_polling_without_cancelling_run(runtime):
    service, _, _ = runtime
    run_id = service.create("demo", status="running")["id"]

    async def consume():
        async def disconnected():
            return True
        frames = [frame async for frame in stream_run_events(service, run_id, is_disconnected=disconnected)]
        assert frames == []

    asyncio.run(consume())
    assert service.get(run_id)["status"] == "running"


def test_reopen_sql_repository_and_recover_interrupted_runs(tmp_path):
    url = f"sqlite+pysqlite:///{(tmp_path / 'restart.db').as_posix()}"
    engine = create_engine(url)
    init_db(engine)
    service = RunService(SqlAlchemyRunRepository(engine))
    completed = service.create("done", status="succeeded")
    unfinished = [service.create("job", status=s) for s in ("queued", "running")]
    service.request_cancel(unfinished[1]["id"])
    engine.dispose()
    reopened = create_engine(url)
    try:
        restored = RunService(SqlAlchemyRunRepository(reopened))
        assert restored.get(completed["id"]) == completed
        assert restored.recover_interrupted() == 2
        assert restored.recover_interrupted() == 0
        for run in unfinished:
            failed = restored.get(run["id"])
            assert failed["status"] == "failed"
            assert failed["error"]["code"] == "RUN_INTERRUPTED"
            assert failed["finished_at"] is not None
            assert failed["events"][-1]["event"] == "run.failed"
        assert restored.get(completed["id"]) == completed
    finally:
        reopened.dispose()


def test_sql_failure_rolls_back_status_and_event(tmp_path):
    from sqlalchemy import event

    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    init_db(engine)
    service = RunService(SqlAlchemyRunRepository(engine))
    run = service.create("demo", status="running")

    def reject_event_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO run_events"):
            raise RuntimeError("simulated event storage failure")

    event.listen(engine, "before_cursor_execute", reject_event_insert)
    try:
        with pytest.raises(RuntimeError, match="event storage failure"):
            service.complete(run["id"], {"type": "message", "id": "uncommitted"})
        assert service.get(run["id"]) == run
    finally:
        event.remove(engine, "before_cursor_execute", reject_event_insert)
        engine.dispose()


def test_queued_run_start_and_failed_terminal(runtime):
    service, _, _ = runtime
    run_id = service.create("demo")["id"]
    assert service.events_for(run_id) == []
    with pytest.raises(DomainConflict):
        service.complete(run_id)
    service.start(run_id)
    service.fail(run_id, {"code": "TEST_FAILURE", "message": "failed", "details": {}})
    failed = service.get(run_id)
    assert failed["status"] == "failed"
    assert failed["finished_at"] is not None
    assert failed["events"][-1]["event"] == "run.failed"
    assert service.request_cancel(run_id) == failed
    assert service.fail(run_id, {"code": "SECOND_FAILURE"}) == failed


def test_sql_run_http_status_and_sse_survive_app_recreation(tmp_path):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app

    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'http.db').as_posix()}")
    init_db(engine)
    try:
        app = create_app(run_service=RunService(SqlAlchemyRunRepository(engine)))
        run = app.state.learning_state.run("material_ingest", {"type": "material_version", "id": "v1"})
        restored = create_app(run_service=RunService(SqlAlchemyRunRepository(engine)))
        with TestClient(restored) as client:
            status = client.get(f"/api/v1/runs/{run['id']}")
            assert status.status_code == 200
            assert status.json()["result_ref"] == run["result_ref"]
            assert "events" not in status.json()
            stream = client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
            assert stream.status_code == 200
            assert "event: run.completed" in stream.text
            assert "event: run.started" not in stream.text
    finally:
        engine.dispose()
