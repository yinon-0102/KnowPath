"""Space metadata survives restarts and concurrent edits honor independent versions."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.db import init_db
from my_agent_llms.learning.materials import InMemoryMaterialRepository, MaterialService
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository


@pytest.fixture(params=["memory", "sql"])
def workspace(request, tmp_path):
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'spaces.db').as_posix()}")
        init_db(engine)
        def factory():
            return create_app(MaterialService(SqlAlchemyMaterialRepository(engine)))
    else:
        repository = InMemoryMaterialRepository()
        def factory():
            return create_app(MaterialService(repository))
    with TestClient(factory()) as client:
        result = client.post("/api/v1/materials", files={"file": ("notes.md", b"# Functions\n\nReusable behavior.", "text/markdown")}, headers={"Idempotency-Key": "material"}).json()
        material_id = result["material"]["id"]
        topic_id = client.get(f"/api/v1/materials/{material_id}/topics").json()["items"][0]["id"]
    yield factory, material_id, topic_id
    if engine:
        engine.dispose()


def create_space(client, material_id, key="space", **extra):
    return client.post("/api/v1/learning-spaces", json={"name": "Python", "material_ids": [material_id], "goal": "Understand functions", "weekly_minutes": 180, **extra}, headers={"Idempotency-Key": key})


def test_space_profile_scope_and_binding_survive_restart(workspace):
    factory, material_id, topic_id = workspace
    with TestClient(factory()) as client:
        response = create_space(client, material_id)
        assert response.status_code == 201
        original = response.json()
        UUID(original["id"])
        path = f"/api/v1/learning-spaces/{original['id']}"
        assert client.patch(path, json={"name": "Python updated", "status": "active", "expected_version": 1}).status_code == 200
        assert client.patch(path + "/profile", json={"goal": "Write reusable code", "preferences": {"example_first": True}, "target_date": "2026-10-01", "expected_version": 1}).status_code == 200
        assert client.post(path + "/scope", json={"topic_ids": [topic_id], "expected_version": 2}, headers={"Idempotency-Key": "scope"}).status_code == 200
        assert client.post(f"/api/v1/materials/{material_id}/versions", files={"file": ("new.md", b"# New topic\n\nNew content.", "text/markdown")}, headers={"Idempotency-Key": "v2"}).status_code == 201
    with TestClient(factory()) as client:
        restored = client.get(path).json()
        assert restored["name"] == "Python updated"
        assert restored["space_version"] == 3
        assert restored["profile_version"] == 2
        assert restored["scope_version"] == 1
        assert restored["bindings"] == original["bindings"]
        assert restored["topic_ids"] == [topic_id]
        assert restored["profile"]["goal"]["value"] == "Write reusable code"
        assert restored["profile"]["preferences"]["source"] == "explicit"
        assert client.get(path + "/profile").json()["profile_version"] == 2
        assert len(client.get("/api/v1/learning-spaces").json()["items"]) == 1
        assert client.get(f"/api/v1/topics/{topic_id}/graph").status_code == 200


def test_replay_returns_original_response_without_reapplying(workspace):
    factory, material_id, topic_id = workspace
    with TestClient(factory()) as client:
        original = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{original['id']}"
        body = {"topic_ids": [topic_id], "expected_version": 1}
        scope = client.post(path + "/scope", json=body, headers={"Idempotency-Key": "scope"}).json()
        assert client.patch(path, json={"name": "Later", "expected_version": 2}).status_code == 200
    with TestClient(factory()) as client:
        assert create_space(client, material_id).json() == original
        assert client.post(path + "/scope", json=body, headers={"Idempotency-Key": "scope"}).json() == scope
        assert client.get(path).json()["space_version"] == 3
        conflict = create_space(client, material_id, name="Changed")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_parallel_creates_and_version_conflicts(workspace):
    factory, material_id, _ = workspace
    clients = [TestClient(factory()) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(lambda c: create_space(c, material_id), clients))
        assert [r.status_code for r in created] == [201, 201]
        assert len({r.json()["id"] for r in created}) == 1
        path = f"/api/v1/learning-spaces/{created[0].json()['id']}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            edited = list(pool.map(lambda c: c.patch(path, json={"name": "Edited", "expected_version": 1}), clients))
        assert sorted(r.status_code for r in edited) == [200, 409]
    finally:
        for client in clients:
            client.close()


def test_scope_rejects_topics_outside_bound_version_and_preserves_versions(workspace):
    factory, material_id, topic_id = workspace
    with TestClient(factory()) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}"
        rejected = client.post(path + "/scope", json={"topic_ids": ["unknown"], "expected_version": 1}, headers={"Idempotency-Key": "scope"})
        assert rejected.status_code == 409
        assert client.get(path).json()["space_version"] == 1
        accepted = client.post(path + "/scope", json={"topic_ids": [topic_id], "expected_version": 1}, headers={"Idempotency-Key": "scope"})
        assert accepted.status_code == 200


@pytest.mark.parametrize("suffix,body", [("", {"name": "bad"}), ("", {"name": "bad", "goal": "wrong endpoint", "expected_version": 1}), ("", {"status": "invalid", "expected_version": 1}), ("/profile", {"weekly_minutes": 0, "expected_version": 1}), ("/profile", {"goal": None, "expected_version": 1}), ("/profile", {"preferences": {"unknown": True}, "expected_version": 1}), ("/profile", {"target_date": "2026-02-30", "expected_version": 1})])
def test_space_writes_validate_contract(workspace, suffix, body):
    factory, material_id, _ = workspace
    with TestClient(factory()) as client:
        space = create_space(client, material_id).json()
        response = client.patch(f"/api/v1/learning-spaces/{space['id']}" + suffix, json=body)
        assert response.status_code == 422
        assert client.get(f"/api/v1/learning-spaces/{space['id']}").json()["space_version"] == 1


@pytest.mark.parametrize("command", ["create", "scope", "profile"])
def test_command_rolls_back_when_replay_record_cannot_be_saved(workspace, monkeypatch, command):
    factory, material_id, topic_id = workspace
    app = factory()
    with TestClient(app) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}"
        before = client.get(path).json()
        repository = app.state.learning_state.space_service.repository
        def fail(*args, **kwargs):
            raise RuntimeError("replay storage failed")
        with monkeypatch.context() as patch:
            patch.setattr(repository, "remember", fail)
            with pytest.raises(RuntimeError, match="replay storage failed"):
                if command == "create":
                    create_space(client, material_id, key="failed", name="Rollback")
                elif command == "scope":
                    client.post(path + "/scope", json={"topic_ids": [topic_id], "expected_version": 1}, headers={"Idempotency-Key": "failed"})
                else:
                    client.patch(path + "/profile", json={"goal": "Rollback", "expected_version": 1}, headers={"Idempotency-Key": "failed"})
        assert client.get(path).json() == before
        assert len(client.get("/api/v1/learning-spaces").json()["items"]) == 1
        assert create_space(client, material_id, key="failed", name="Retry").status_code == 201


def test_cross_endpoint_keys_conflict_in_both_directions(workspace):
    factory, material_id, _ = workspace
    with TestClient(factory()) as client:
        assert create_space(client, material_id, key="material").status_code == 409
        assert create_space(client, material_id).status_code == 201
        conflict = client.post("/api/v1/materials", files={"file": ("other.md", b"# Other", "text/markdown")}, headers={"Idempotency-Key": "space"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_keyed_profile_replay_and_preference_deletion(workspace):
    factory, material_id, _ = workspace
    with TestClient(factory()) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}"
        body = {"preferences": {"example_first": True, "concise_explanations": False}, "expected_version": 1}
        first = client.patch(path + "/profile", json=body, headers={"Idempotency-Key": "profile"})
        assert first.status_code == 200
        assert client.patch(path + "/profile", json={"preferences": {"example_first": None}, "target_date": None, "expected_version": 2}).status_code == 200
    with TestClient(factory()) as client:
        replay = client.patch(path + "/profile", json=body, headers={"Idempotency-Key": "profile"})
        assert replay.json() == first.json()
        current = client.get(path).json()
        assert current["space_version"] == 1
        assert current["profile_version"] == 3
        assert current["profile"]["preferences"]["value"] == {"concise_explanations": False}
        assert client.patch(path + "/profile", json={"preferences": None, "expected_version": 3}).status_code == 200
        assert "preferences" not in client.get(path + "/profile").json()["profile"]


def test_scope_keeps_old_binding_and_invalidates_only_old_plans(workspace):
    factory, material_id, topic_id = workspace
    app = factory()
    with TestClient(app) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}"
        old_plan = app.state.learning_state.create_plan(space["id"], {})
        upload = client.post(f"/api/v1/materials/{material_id}/versions", files={"file": ("new.md", b"# New topic\n\nNew content.", "text/markdown")}, headers={"Idempotency-Key": "v2"})
        assert upload.status_code == 201
        new_topic = client.get(f"/api/v1/materials/{material_id}/topics").json()["items"][0]["id"]
        assert new_topic != topic_id
        assert client.post(path + "/scope", json={"topic_ids": [new_topic], "expected_version": 1}, headers={"Idempotency-Key": "scope"}).status_code == 409
        body = {"topic_ids": [topic_id], "expected_version": 1}
        assert client.post(path + "/scope", json=body, headers={"Idempotency-Key": "scope"}).status_code == 200
        assert app.state.learning_state.get_plan(old_plan["id"])["status"] == "needs_replan"
        new_plan = app.state.learning_state.create_plan(space["id"], {})
        assert client.post(path + "/scope", json=body, headers={"Idempotency-Key": "scope"}).status_code == 200
        assert app.state.learning_state.get_plan(new_plan["id"])["status"] == "ready"


def test_bound_material_cannot_be_deleted_after_restart(workspace):
    factory, material_id, _ = workspace
    with TestClient(factory()) as client:
        assert create_space(client, material_id).status_code == 201
    with TestClient(factory()) as client:
        for cascade in (False, True):
            response = client.request("DELETE", f"/api/v1/materials/{material_id}", json={"cascade": cascade})
            assert response.status_code == 409
        assert client.get(f"/api/v1/materials/{material_id}").status_code == 200


def test_create_replay_is_tombstoned_after_resource_deletion(workspace):
    factory, material_id, _ = workspace
    app = factory()
    with TestClient(app) as client:
        original = create_space(client, material_id)
        app.state.learning_state.delete_space(original.json()["id"], {"confirm": True, "expected_version": 1})
        replay = create_space(client, material_id)
        assert replay.status_code == 410
        assert replay.json()["error"]["code"] == "RESOURCE_DELETED"
        assert client.get("/api/v1/learning-spaces").json()["items"] == []


def test_scope_replay_is_tombstoned_after_resource_deletion(workspace):
    factory, material_id, topic_id = workspace
    app = factory()
    with TestClient(app) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}/scope"
        body = {"topic_ids": [topic_id], "expected_version": 1}
        headers = {"Idempotency-Key": "scope"}
        original = client.post(path, json=body, headers=headers)
        assert original.status_code == 200
        app.state.learning_state.delete_space(space["id"], {"confirm": True, "expected_version": app.state.learning_state.get_space(space["id"])["space_version"]})
        replay = client.post(path, json=body, headers=headers)
        assert replay.status_code == 410
        assert replay.json()["error"]["code"] == "RESOURCE_DELETED"


def test_plan_read_detects_scope_changed_by_another_application(workspace):
    factory, material_id, topic_id = workspace
    original_app = factory()
    with TestClient(original_app) as client:
        space = create_space(client, material_id).json()
        plan = original_app.state.learning_state.create_plan(space["id"], {})
        with TestClient(factory()) as other:
            assert other.post(f"/api/v1/learning-spaces/{space['id']}/scope", json={"topic_ids": [topic_id], "expected_version": 1}, headers={"Idempotency-Key": "scope"}).status_code == 200
        assert client.get(f"/api/v1/plans/{plan['id']}").json()["status"] == "needs_replan"


def test_existing_knowledge_update_stub_keeps_version_in_repository(workspace):
    factory, material_id, _ = workspace
    with TestClient(factory()) as client:
        space = create_space(client, material_id).json()
        path = f"/api/v1/learning-spaces/{space['id']}"
        response = client.post(path + "/knowledge-updates/apply", json={"expected_space_version": 1}, headers={"Idempotency-Key": "knowledge"})
        assert response.status_code == 202
        assert response.json()["space_version"] == 2
        assert client.get(path).json()["space_version"] == 2


def test_bound_topics_keep_material_identity_without_duplicate_aliases(workspace):
    factory, material_id, _ = workspace
    app = factory()
    with TestClient(app) as client:
        uploaded = client.post("/api/v1/materials", files={"file": ("other.md", b"# Functions\n\nDifferent source.", "text/markdown")}, headers={"Idempotency-Key": "other"})
        assert uploaded.status_code == 201
        other_id = uploaded.json()["material"]["id"]
        space = client.post("/api/v1/learning-spaces", json={"name": "Python", "material_ids": [material_id, other_id], "goal": "Understand functions", "weekly_minutes": 180}, headers={"Idempotency-Key": "multi-space"}).json()
        topics = app.state.learning_state.topics_for_space(space["id"])
        assert len(topics) == 2
        assert len({topic["id"] for topic in topics}) == 2
        assert {topic["source_refs"][0]["material_id"] for topic in topics} == {material_id, other_id}
        assert all(len(topic["source_refs"]) == 1 for topic in topics)
