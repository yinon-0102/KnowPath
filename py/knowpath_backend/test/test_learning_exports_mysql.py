"""MySQL current-read export snapshot after waiting on a concurrent writer."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import delete

from knowpath_backend.learning.persistence.db import ExportRow, IdempotencyRow
from knowpath_backend.test.test_learning_plans_mysql import plans_workspace
from knowpath_backend.test.test_learning_state_persistence import workspace


def test_export_includes_plans_committed_after_initial_snapshot(plans_workspace, monkeypatch):
    factory, space_id, _, engine = plans_workspace
    writer, reader = factory(), factory()
    reached = Event()
    original = reader.space_service.repository.get
    def before_space_lock(identifier):
        reached.set()
        return original(identifier)
    monkeypatch.setattr(reader.space_service.repository, "get", before_space_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with writer.assessment_service.repository.transaction():
                plan = writer.create_plan(space_id, {})
                session = writer.start_session(plan["id"], plan["tasks"][0]["id"])
                future = pool.submit(reader.create_export, space_id, {}, "export-" + space_id)
                assert reached.wait(10)
            result = future.result(timeout=15)
        restored = factory()
        snapshot = restored.export_payload(result["export_id"])
        assert [p["id"] for p in snapshot["plans"]] == [plan["id"]]
        assert [s["id"] for s in snapshot["sessions"]] == [session["id"]]
        assert restored.create_export(space_id, {}, "export-" + space_id) == result
    finally:
        with engine.begin() as connection:
            connection.execute(delete(ExportRow).where(ExportRow.space_id == space_id))
            connection.execute(delete(IdempotencyRow).where(IdempotencyRow.key == "export-" + space_id))
