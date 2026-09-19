"""Opt-in integration check against a migrated MySQL database.

Set LEARNING_TEST_MYSQL_URL explicitly. Only runs created by this test are deleted.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.db import RunRow
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.run_repository import SqlAlchemyRunRepository
from knowpath_backend.learning.runs import RunService


@pytest.mark.skipif(not os.getenv("LEARNING_TEST_MYSQL_URL"), reason="requires explicit MySQL test URL")
def test_mysql_run_transactions_concurrency_and_http_replay():
    engine = create_engine(os.environ["LEARNING_TEST_MYSQL_URL"], pool_pre_ping=True)
    assert engine.dialect.name == "mysql"
    services = [RunService(SqlAlchemyRunRepository(engine)) for _ in range(2)]
    created_ids = []
    try:
        run_id = services[0].create("integration_check", status="running")["id"]
        created_ids.append(run_id)
        # Distinct repositories have distinct Python locks; MySQL must serialize writes.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: services[i % 2].append_event(run_id, "progress", {"percent": i}), range(1, 13)))
        services[0].complete(run_id, {"type": "integration_check", "id": "ready"})
        events = services[1].events_for(run_id)
        assert [int(e["id"]) for e in events] == list(range(1, 15))
        assert events[-1]["event"] == "run.completed"
        with TestClient(create_app(run_service=services[1])) as client:
            assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "succeeded"
            response = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "13"})
            assert response.status_code == 200
            assert "id: 14" in response.text
            assert "event: run.completed" in response.text
        cancelled_id = services[0].create("integration_cancel", status="running")["id"]
        created_ids.append(cancelled_id)
        services[1].request_cancel(cancelled_id)
        with pytest.raises(DomainConflict):
            services[0].complete(cancelled_id, {"type": "message", "id": "late"})
        assert services[1].get(cancelled_id)["result_ref"] is None
        services[0].acknowledge_cancel(cancelled_id)
        assert services[1].get(cancelled_id)["status"] == "cancelled"
    finally:
        if created_ids:
            with engine.begin() as connection:
                connection.execute(delete(RunRow).where(RunRow.id.in_(created_ids)))
        engine.dispose()
