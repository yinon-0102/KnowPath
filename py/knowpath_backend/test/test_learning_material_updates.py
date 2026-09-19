"""Persistent metadata versions and archive boundaries."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.test.test_learning_state_persistence import workspace


def material_id(state, space_id):
    return state.get_space(space_id)["bindings"][0]["material_id"]


def test_update_survives_restart_and_stale_version_is_rejected(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    identifier = material_id(state, space_id)
    updated = state.material_service.update(identifier, {"name": "Renamed", "expected_version": 1})
    assert updated.version == 2
    restored = factory().material_repository.get_material(identifier)
    assert restored.name == "Renamed"
    assert restored.version == 2
    with pytest.raises(DomainConflict) as exc:
        factory().material_service.update(identifier, {"name": "Lost", "expected_version": 1})
    assert exc.value.code == "VERSION_CONFLICT"
    assert factory().material_repository.get_material(identifier).name == "Renamed"


def test_archive_prevents_new_bindings_but_preserves_existing_space(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    identifier = material_id(state, space_id)
    before = state.topics_for_space(space_id)
    state.material_service.update(identifier, {"status": "archived", "expected_version": 1})
    assert factory().topics_for_space(space_id) == before
    with pytest.raises(DomainConflict) as exc:
        factory().create_space({"name": "Forbidden", "material_ids": [identifier]})
    assert exc.value.code == "MATERIAL_ARCHIVED"


def test_concurrent_patch_has_one_version_winner(workspace):
    factory, space_id, _, _ = workspace
    identifier = material_id(factory(), space_id)
    def update(name):
        try:
            return factory().material_service.update(identifier, {"name": name, "expected_version": 1}).version
        except DomainConflict as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("First", "Second")))
    assert sorted(map(str, results)) == ["2", "VERSION_CONFLICT"]


def test_http_material_metadata_validation_and_public_version():
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material").material
    url = f"/api/v1/materials/{material.id}"
    with TestClient(app) as client:
        assert client.get(url).json()["material"]["version"] == 1
        for invalid in ({"name": "New"}, {"name": "", "expected_version": 1},
                        {"name": None, "expected_version": 1}, {"status": "ready", "expected_version": 1},
                        {"size_bytes": 0, "expected_version": 1}, {"name": "New", "expected_version": True}):
            assert client.patch(url, json=invalid).status_code == 422
        result = client.patch(url, json={"name": "New", "expected_version": 1})
        assert result.status_code == 200
        assert result.json()["version"] == 2
        assert client.get(url).json()["material"]["name"] == "New"
        assert client.patch(url, json={"name": "Stale", "expected_version": 1}).status_code == 409


def test_content_revision_increments_metadata_version_and_preserves_archive(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    identifier = material_id(state, space_id)
    state.material_service.update(identifier, {"status": "archived", "expected_version": 1})
    kwargs = {"material_id": identifier, "filename": "new.md", "content": b"# Functions\n\nNew content " + identifier.encode(), "idempotency_key": identifier + "-version"}
    result = state.material_service.create_version(**kwargs)
    assert result.material.version == 3
    assert result.material.status == "archived"
    assert state.material_service.create_version(**kwargs).material.version == 3
    assert factory().material_repository.get_material(identifier).version == 3
