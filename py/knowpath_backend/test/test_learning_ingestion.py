"""Upload idempotency must survive application recreation and partial failures."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.db import init_db
from knowpath_backend.learning.materials import InMemoryMaterialRepository, MaterialService
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository
from knowpath_backend.learning.runs import RunService
from knowpath_backend.test.material_upload_helpers import complete_upload


@pytest.fixture(params=["memory", "sql"])
def apps(request, tmp_path):
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'ingestion.db').as_posix()}")
        init_db(engine)
        def factory():
            return create_app(MaterialService(SqlAlchemyMaterialRepository(engine)))
    else:
        repository = InMemoryMaterialRepository()
        def factory():
            return create_app(MaterialService(repository))
    yield factory
    if engine is not None:
        engine.dispose()


def upload(client, key="upload-one", content=b"# Notes\n\nTransaction basics.", path="/api/v1/materials"):
    return client.post(path, files={"file": ("notes.md", content, "text/markdown")},
                       headers={"Idempotency-Key": key})


def test_upload_replay_survives_application_recreation(apps):
    with TestClient(apps()) as client:
        first = upload(client)
        assert first.status_code == 201
    with TestClient(apps()) as client:
        replay = upload(client)
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        assert replay.json()["material"]["id"] == first.json()["material"]["id"]
        assert replay.json()["version"]["id"] == first.json()["version"]["id"]
        assert replay.json()["run_id"] == first.json()["run_id"]
        run_id = replay.json()["run_id"]
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "queued"
        complete_upload(client, replay)
        assert client.get(f"/api/v1/runs/{run_id}/events").text.count("event: run.completed") == 1
        assert upload(client, content=b"different payload").status_code == 409


def test_version_replay_keeps_original_run_and_does_not_revert_current_version(apps):
    with TestClient(apps()) as client:
        material_id = upload(client).json()["material"]["id"]
        path = f"/api/v1/materials/{material_id}/versions"
        second = upload(client, "version-two", b"Second version", path).json()
        third = upload(client, "version-three", b"Third version", path).json()
    with TestClient(apps()) as client:
        replay = upload(client, "version-two", b"Second version", path)
        assert replay.status_code == 201
        assert replay.json()["material_version_id"] == second["material_version_id"]
        assert replay.json()["run_id"] == second["run_id"]
        material = client.get(f"/api/v1/materials/{material_id}").json()
        assert material["material"]["current_version_id"] == third["material_version_id"]


def test_run_write_failure_rolls_back_entire_upload(apps, monkeypatch):
    app = apps()
    repo = app.state.learning_state.material_repository
    def fail_run_write(run):
        raise RuntimeError("simulated run write failure")
    with monkeypatch.context() as patch:
        patch.setattr(app.state.learning_state.run_service.repository, "create", fail_run_write)
        with TestClient(app) as client:
            assert upload(client).status_code == 500
    assert repo.list_materials() == []
    assert repo.get_idempotency("upload-one") is None
    with TestClient(apps()) as client:
        retry = upload(client)
        assert retry.status_code == 201
        assert retry.json()["replayed"] is False


def test_parallel_retries_have_one_material_and_run(apps):
    # Separate applications exercise repository coordination, not one HTTP handler's dictionary.
    clients = [TestClient(apps()) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(upload, clients))
        assert [response.status_code for response in responses] == [201] * 4
        assert len({response.json()["run_id"] for response in responses}) == 1
        assert len(clients[0].get("/api/v1/materials").json()["items"]) == 1
    finally:
        for client in clients:
            client.close()


def test_binding_failure_rolls_back_material_and_created_run(apps, monkeypatch):
    app = apps()
    state = app.state.learning_state
    created_run_ids = []
    original_create = state.run_service.repository.create
    def capture_create(run):
        created_run_ids.append(run["id"])
        original_create(run)
    def fail_bind(key, run_id):
        raise RuntimeError("simulated binding failure")
    monkeypatch.setattr(state.run_service.repository, "create", capture_create)
    monkeypatch.setattr(state.material_repository, "bind_idempotency_run", fail_bind)
    with TestClient(app) as client:
        assert upload(client).status_code == 500
    assert created_run_ids
    assert state.material_repository.list_materials() == []
    assert state.material_repository.get_idempotency("upload-one") is None
    from knowpath_backend.learning.errors import DomainNotFound
    with pytest.raises(DomainNotFound):
        state.run_service.get(created_run_ids[0])


def test_failed_version_upload_keeps_previous_version(apps, monkeypatch):
    app = apps()
    repo = app.state.learning_state.material_repository
    with TestClient(app) as client:
        original = upload(client).json()
        material_id = original["material"]["id"]
        def fail_bind(key, run_id):
            raise RuntimeError("simulated version binding failure")
        with monkeypatch.context() as patch:
            patch.setattr(repo, "bind_idempotency_run", fail_bind)
            assert upload(client, "failed-version", b"new version", f"/api/v1/materials/{material_id}/versions").status_code == 500
        assert repo.get_material(material_id).current_version_id == original["version"]["id"]
        assert len(repo.list_versions(material_id)) == 1
        assert repo.get_idempotency("failed-version") is None


def test_delete_then_reupload_same_content_does_not_reuse_deleted_material(apps):
    with TestClient(apps()) as client:
        original = upload(client).json()
        assert client.request("DELETE", f"/api/v1/materials/{original['material']['id']}", json={"expected_version": 1, "confirm": True}).status_code == 202
        replacement = upload(client, "new-upload")
        assert replacement.status_code == 201
        assert replacement.json()["material"]["id"] != original["material"]["id"]
