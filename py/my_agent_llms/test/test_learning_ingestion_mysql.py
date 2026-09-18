"""Explicit opt-in MySQL upload test; remove only records created by this test."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from threading import Barrier, Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.db import (GraphRevisionRow, IdempotencyRow, MaterialRow,
    MaterialVersionRow, OutboxEventRow, RunRow, SourceChunkRow)
from my_agent_llms.learning.materials import MaterialService
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository
from my_agent_llms.test.material_upload_helpers import complete_upload


@pytest.mark.skipif(not os.getenv("LEARNING_TEST_MYSQL_URL"), reason="requires explicit MySQL test URL")
@pytest.mark.parametrize("scenario", ["new", "legacy", "different_keys", "stale_snapshot"])
def test_mysql_upload_concurrent_replay_and_restart(scenario, monkeypatch):
    engine = create_engine(os.environ["LEARNING_TEST_MYSQL_URL"], pool_pre_ping=True)
    assert engine.dialect.name == "mysql"
    label = f"ingestion-test-{uuid4().hex}"
    key = f"{label}-upload"
    version_key = f"{label}-version"
    upload_keys = [key] + [f"{key}-{i}" for i in range(1, 4)] if scenario in {"different_keys", "stale_snapshot"} else [key] * 4
    clients = []
    def new_client():
        client = TestClient(create_app(MaterialService(SqlAlchemyMaterialRepository(engine))))
        clients.append(client)
        return client
    def send(client, *, path="/api/v1/materials", request_key=key, text=None):
        return client.post(path, files={"file": (f"{label}.txt", (text or label).encode(), "text/plain")},
                           headers={"Idempotency-Key": request_key})
    try:
        if scenario == "legacy":
            service = MaterialService(SqlAlchemyMaterialRepository(engine))
            service.create(filename=f"{label}.txt", content=label.encode(),
                           idempotency_key=key)
            # All four transactions read the legacy NULL association before
            # one can bind a Run, reproducing a REPEATABLE READ stale snapshot.
            barrier, guard = Barrier(4), Lock()
            reads = 0
            original_get = SqlAlchemyMaterialRepository.get_idempotency
            def synchronized_get(repository, request_key):
                nonlocal reads
                result = original_get(repository, request_key)
                with guard:
                    wait = request_key == key and reads < 4
                    if wait:
                        reads += 1
                if wait:
                    barrier.wait(timeout=15)
                return result
            monkeypatch.setattr(SqlAlchemyMaterialRepository, "get_idempotency", synchronized_get)
        if scenario == "different_keys":
            barrier, guard = Barrier(4), Lock()
            reads = 0
            original_find = SqlAlchemyMaterialRepository.find_by_content_hash
            def synchronized_find(repository, content_hash, **kwargs):
                nonlocal reads
                result = original_find(repository, content_hash, **kwargs)
                with guard:
                    wait = reads < 4
                    reads += 1
                if wait:
                    barrier.wait(timeout=15)
                return result
            monkeypatch.setattr(SqlAlchemyMaterialRepository, "find_by_content_hash", synchronized_find)
        first_clients = [new_client() for _ in range(4)]
        if scenario == "stale_snapshot":
            snapshot_ready, winner_committed = Event(), Event()
            original_get = SqlAlchemyMaterialRepository.get_idempotency
            def pause_snapshot(repository, request_key):
                result = original_get(repository, request_key)
                if request_key == upload_keys[1]:
                    snapshot_ready.set()
                    assert winner_committed.wait(timeout=15)
                return result
            monkeypatch.setattr(SqlAlchemyMaterialRepository, "get_idempotency", pause_snapshot)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(send, first_clients[1], request_key=upload_keys[1])
                try:
                    assert snapshot_ready.wait(timeout=15)
                    winner = send(first_clients[0])
                finally:
                    winner_committed.set()
                follower = pending.result(timeout=15)
            responses = [winner, follower] + [send(first_clients[i], request_key=upload_keys[i]) for i in (2, 3)]
        else:
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(lambda pair: send(pair[0], request_key=pair[1]), zip(first_clients, upload_keys)))
        assert [response.status_code for response in responses] == [201] * 4
        assert len({response.json()["material"]["id"] for response in responses}) == 1
        assert len({response.json()["version"]["id"] for response in responses}) == 1
        assert len({response.json()["run_id"] for response in responses}) == 1
        assert {response.json()["version"]["chunk_count"] for response in responses} == ({1} if scenario == "legacy" else {0})
        first = responses[0].json()
        restored = new_client()
        replay = send(restored).json()
        assert replay["run_id"] == first["run_id"]
        complete_upload(restored, responses[0])
        assert restored.get(f"/api/v1/runs/{replay['run_id']}/events").text.count("event: run.completed") == 1
        assert send(restored, text="conflicting payload").status_code == 409
        path = f"/api/v1/materials/{first['material']['id']}/versions"
        version = send(restored, path=path, request_key=version_key, text=label + " v2")
        assert version.status_code == 201
        version_replay = send(new_client(), path=path, request_key=version_key, text=label + " v2")
        assert version_replay.json()["run_id"] == version.json()["run_id"]
        with engine.connect() as connection:
            assert len(connection.scalars(select(MaterialRow.id).where(MaterialRow.name == label)).all()) == 1
            assert len(connection.scalars(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id == first['material']['id'])).all()) == 2
    finally:
        for client in clients:
            client.close()
        with engine.begin() as connection:
            run_ids = connection.scalars(select(IdempotencyRow.run_id).where(IdempotencyRow.key.in_([*upload_keys, version_key]))).all()
            material_ids = connection.scalars(select(MaterialRow.id).where(MaterialRow.name == label)).all()
            version_ids = connection.scalars(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id.in_(material_ids))).all()
            connection.execute(delete(IdempotencyRow).where(IdempotencyRow.key.in_([*upload_keys, version_key])))
            revision_ids = connection.scalars(select(GraphRevisionRow.id).where(GraphRevisionRow.material_id.in_(material_ids))).all()
            connection.execute(delete(OutboxEventRow).where(OutboxEventRow.aggregate_id.in_([*version_ids, *revision_ids])))
            connection.execute(delete(GraphRevisionRow).where(GraphRevisionRow.id.in_(revision_ids)))
            connection.execute(delete(SourceChunkRow).where(SourceChunkRow.material_version_id.in_(version_ids)))
            connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.id.in_(version_ids)))
            connection.execute(delete(MaterialRow).where(MaterialRow.id.in_(material_ids)))
            connection.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))
        engine.dispose()
