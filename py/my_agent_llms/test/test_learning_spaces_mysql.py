"""Opt-in MySQL regressions for space transactions and repeatable-read races."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from my_agent_llms.test.material_upload_helpers import ParsedUploadClient
from sqlalchemy import create_engine, delete, select

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.db import (GraphRevisionRow, IdempotencyRow, LearningSpaceRow,
    MaterialRow, MaterialVersionRow, OutboxEventRow, RunRow, SourceChunkRow)
from my_agent_llms.learning.materials import MaterialService
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository


@pytest.fixture
def mysql_workspace():
    url = os.getenv("LEARNING_TEST_MYSQL_URL")
    if not url:
        pytest.skip("requires explicit MySQL test URL")
    engine = create_engine(url, pool_pre_ping=True)
    assert engine.dialect.name == "mysql"
    label = f"space-test-{uuid4().hex}"
    clients = []
    def client():
        result = ParsedUploadClient(create_app(MaterialService(SqlAlchemyMaterialRepository(engine))))
        clients.append(result)
        return result
    try:
        first = client()
        uploaded = first.post("/api/v1/materials", files={"file": (label + ".md", f"# Functions\n\n{label}".encode(), "text/markdown")}, headers={"Idempotency-Key": label + "-upload"})
        assert uploaded.status_code == 201
        material_id = uploaded.json()["material"]["id"]
        body = {"name": label, "material_ids": [material_id], "goal": "Use functions"}
        yield client, label, material_id, body
    finally:
        for item in clients:
            item.close()
        # Only this fixture's unique keys/names are touched; shared developer data remains.
        with engine.begin() as connection:
            keys = IdempotencyRow.key.startswith(label)
            run_ids = connection.scalars(select(IdempotencyRow.run_id).where(keys)).all()
            material_ids = connection.scalars(select(MaterialRow.id).where(MaterialRow.name == label)).all()
            versions = connection.scalars(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id.in_(material_ids))).all()
            revisions = connection.scalars(select(GraphRevisionRow.id).where(GraphRevisionRow.material_id.in_(material_ids))).all()
            connection.execute(delete(LearningSpaceRow).where(LearningSpaceRow.name == label))
            connection.execute(delete(IdempotencyRow).where(keys))
            connection.execute(delete(OutboxEventRow).where(OutboxEventRow.aggregate_id.in_([*versions, *revisions])))
            connection.execute(delete(GraphRevisionRow).where(GraphRevisionRow.id.in_(revisions)))
            connection.execute(delete(SourceChunkRow).where(SourceChunkRow.material_version_id.in_(versions)))
            connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.material_id.in_(material_ids)))
            connection.execute(delete(MaterialRow).where(MaterialRow.id.in_(material_ids)))
            connection.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))
        engine.dispose()


def rendezvous_replays(clients, monkeypatch):
    barrier = Barrier(len(clients))
    for client in clients:
        repository = client.app.state.learning_state.space_service.repository
        original = repository.replay
        def wrapper(key, fingerprint, original=original, reached=[]):
            result = original(key, fingerprint)
            if not reached:
                reached.append(True)
                barrier.wait(timeout=10)
            return result
        monkeypatch.setattr(repository, "replay", wrapper)


@pytest.mark.parametrize("command", ["create", "scope", "profile", "update"])
def test_mysql_same_key_concurrent_commands_replay_exact_response(mysql_workspace, monkeypatch, command):
    factory, label, material_id, body = mysql_workspace
    clients = [factory(), factory()]
    first = clients[0].post("/api/v1/learning-spaces", json=body, headers={"Idempotency-Key": label + "-seed"})
    assert first.status_code == 201
    space_id = first.json()["id"]
    path = f"/api/v1/learning-spaces/{space_id}"
    topic_id = clients[0].get(f"/api/v1/materials/{material_id}/topics").json()["items"][0]["id"]
    rendezvous_replays(clients, monkeypatch)
    def send(client):
        headers = {"Idempotency-Key": label + "-command"}
        if command == "create":
            return client.post("/api/v1/learning-spaces", json=body, headers=headers)
        if command == "scope":
            return client.post(path + "/scope", json={"topic_ids": [topic_id], "expected_version": 1}, headers=headers)
        if command == "profile":
            return client.patch(path + "/profile", json={"weekly_minutes": 300, "expected_version": 1}, headers=headers)
        return client.patch(path, json={"status": "active", "expected_version": 1}, headers=headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, clients))
    assert [r.status_code for r in responses] == ([201, 201] if command == "create" else [200, 200])
    assert responses[0].json() == responses[1].json()
    restarted = factory()
    assert send(restarted).json() == responses[0].json()
    current = restarted.get(path).json()
    assert current["space_version"] == (2 if command in {"scope", "update"} else 1)
    assert current["profile_version"] == (2 if command == "profile" else 1)


def test_mysql_different_keys_cannot_overwrite_same_version(mysql_workspace):
    factory, label, _, body = mysql_workspace
    clients = [factory(), factory()]
    created = clients[0].post("/api/v1/learning-spaces", json=body, headers={"Idempotency-Key": label + "-space"}).json()
    path = f"/api/v1/learning-spaces/{created['id']}"
    def send(pair):
        index, client = pair
        return client.patch(path, json={"status": "active", "expected_version": 1}, headers={"Idempotency-Key": f"{label}-edit-{index}"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, enumerate(clients)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert factory().get(path).json()["space_version"] == 2


def test_mysql_space_creation_reads_version_published_after_old_snapshot(mysql_workspace, monkeypatch):
    factory, label, material_id, body = mysql_workspace
    client = factory()
    repository = client.app.state.learning_state.space_service.repository
    original = repository.replay
    snapshot_ready, upload_done = Event(), Event()
    def replay(key, fingerprint):
        result = original(key, fingerprint)
        snapshot_ready.set()
        assert upload_done.wait(timeout=10)
        return result
    monkeypatch.setattr(repository, "replay", replay)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, "/api/v1/learning-spaces", json=body, headers={"Idempotency-Key": label + "-space"})
        try:
            assert snapshot_ready.wait(timeout=10)
            publishing = factory()
            uploaded = publishing.post(f"/api/v1/materials/{material_id}/versions", files={"file": (label + "-v2.md", f"# Parameters\n\n{label}".encode(), "text/markdown")}, headers={"Idempotency-Key": label + "-version"})
            assert uploaded.status_code == 201
            graph = publishing.app.state.learning_state.graph_service
            candidate = next(row for row in graph._history(material_id)
                             if row["run_id"] == uploaded.json()["run_id"])
            published = graph.publish(material_id, candidate["id"],
                                      {"expected_graph_version": 1, "resolutions": []},
                                      label + "-publish-v2")
            assert published["graph_version"] == 2
        finally:
            upload_done.set()
        response = pending.result(timeout=10)
    assert response.status_code == 201
    version_id = uploaded.json()["material_version_id"]
    assert response.json()["bindings"][0]["material_version_id"] == version_id


def test_mysql_delete_waits_for_creation_and_rejects_bound_material(mysql_workspace, monkeypatch):
    factory, label, material_id, body = mysql_workspace
    creating, deleting = factory(), factory()
    materials = creating.app.state.learning_state.material_repository
    original = materials.get_material
    locked, release, deleting_started = Event(), Event(), Event()
    def hold(material_id):
        result = original(material_id)
        locked.set()
        assert release.wait(timeout=10)
        return result
    monkeypatch.setattr(materials, "get_material", hold)
    delete_repo = deleting.app.state.learning_state.material_repository
    original_delete_get = delete_repo.get_material
    def signal(material_id):
        deleting_started.set()
        return original_delete_get(material_id)
    monkeypatch.setattr(delete_repo, "get_material", signal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(creating.post, "/api/v1/learning-spaces", json=body, headers={"Idempotency-Key": label + "-space"})
        try:
            assert locked.wait(timeout=10)
            deletion = pool.submit(deleting.request, "DELETE", f"/api/v1/materials/{material_id}",
                                   json={"expected_version": 1, "confirm": True})
            assert deleting_started.wait(timeout=10)
        finally:
            release.set()
        assert create.result(timeout=10).status_code == 201
        assert deletion.result(timeout=10).status_code == 409
    assert factory().get(f"/api/v1/materials/{material_id}").status_code == 200
