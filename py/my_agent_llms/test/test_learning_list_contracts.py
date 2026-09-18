"""Public list contracts: bounded stable pages, scoped cursors and version metadata."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.db import init_db
from my_agent_llms.learning.materials import MaterialService, InMemoryMaterialRepository
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository


@pytest.fixture(params=["memory", "sql"])
def setup(request, tmp_path):
    engine = None
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'lists.db').as_posix()}")
        init_db(engine)
        repo = SqlAlchemyMaterialRepository(engine)
    else:
        repo = InMemoryMaterialRepository()
    service = MaterialService(repo)
    results = [service.create(filename=f"notes-{i}.md", content=f"# Topic {i}\n\nFact {i}".encode(), idempotency_key=f"material-{i}") for i in range(25)]
    app = create_app(service)
    with TestClient(app) as client:
        yield client, service, results, app.state.learning_state
    if engine:
        engine.dispose()


def collect(client, path, **params):
    rows, cursor = [], None
    for _ in range(100):
        response = client.get(path, params={**params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        rows.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows
    pytest.fail("pagination did not terminate")


def test_material_default_limit_and_filter(setup):
    client, service, results, _ = setup
    response = client.get("/api/v1/materials").json()
    assert len(response["items"]) == 20
    assert response["next_cursor"]
    all_rows = collect(client, "/api/v1/materials", limit=7)
    assert len(all_rows) == len({row["id"] for row in all_rows}) == 25
    archived = service.update(results[0].material.id, {"status": "archived", "expected_version": 1})
    assert [row["id"] for row in collect(client, "/api/v1/materials", status="archived")] == [archived.id]
    assert len(collect(client, "/api/v1/materials", status="ready")) == 24


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"limit": "oops"}, {"cursor": "bad-cursor"}, {"status": "invalid"}])
def test_invalid_material_queries_are_rejected(setup, query):
    client, *_ = setup
    assert client.get("/api/v1/materials", params=query).status_code == 422


def test_cursor_is_bound_to_filter_and_resource(setup):
    client, service, results, state = setup
    page = client.get("/api/v1/materials", params={"limit": 2}).json()
    cursor = page["next_cursor"]
    assert cursor is not None
    assert client.get("/api/v1/materials", params={"status": "ready", "cursor": cursor}).status_code == 422
    assert client.get("/api/v1/learning-spaces", params={"cursor": cursor}).status_code == 422
    # Deleting an already emitted item must not shift the next page.
    service.repository.delete_material(page["items"][0]["id"])
    rest = collect(client, "/api/v1/materials", cursor=cursor, limit=3)
    assert not {row["id"] for row in rest} & {row["id"] for row in page["items"]}
    assert len(rest) == 23


def test_versions_and_spaces_paginate(setup):
    client, service, results, state = setup
    material_id = results[0].material.id
    for i in range(4):
        service.create_version(material_id=material_id, filename="new.md", content=f"# Revision {i}\n\nDifferent.".encode(), idempotency_key=f"version-{i}")
    versions = collect(client, f"/api/v1/materials/{material_id}/versions", limit=2)
    assert len(versions) == len({row["id"] for row in versions}) == 5
    assert all("graph_version" in row for row in versions)
    for i in range(4):
        state.create_space({"name": f"Space {i}", "material_ids": [material_id]}, idempotency_key=f"space-{i}")
    spaces = collect(client, "/api/v1/learning-spaces", limit=2)
    assert len(spaces) == len({row["id"] for row in spaces}) == 4
    cursor = client.get(f"/api/v1/materials/{material_id}/versions", params={"limit": 2}).json()["next_cursor"]
    other = results[1].material.id
    assert client.get(f"/api/v1/materials/{other}/versions", params={"cursor": cursor}).status_code == 422
