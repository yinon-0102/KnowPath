"""Frozen HTTP and MySQL contracts captured before the directory refactor."""

import json
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.api.contract import ContractRoute
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.rag.retrieval import KeywordRetriever


FIXTURES = Path(__file__).parent / "fixtures"


def mysql_schema_contract():
    """Compile offline: preserve columns, constraints, defaults and indexes."""
    dialect = mysql.dialect()
    return {
        table.name: {
            "table": str(CreateTable(table).compile(dialect=dialect)),
            "indexes": sorted(
                str(CreateIndex(index).compile(dialect=dialect))
                for index in table.indexes
            ),
        }
        for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    }


def isolated_app(*, token=None):
    return create_app(
        settings=LearningSettings(local_token=token),
        source_retriever=KeywordRetriever(),
    )


def test_full_openapi_matches_pre_refactor_contract():
    expected = json.loads((FIXTURES / "learning_openapi.json").read_text(encoding="utf-8"))
    app = isolated_app()

    assert app.openapi() == expected
    def api_routes(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif (included := getattr(route, "original_router", None)) is not None:
                # FastAPI 0.141+ retains included routers instead of flattening.
                yield from api_routes(included.routes)

    routes = list(api_routes(app.routes))
    assert len(routes) == 49
    assert all(isinstance(route, ContractRoute) for route in routes)
    assert len({(route.path, method) for route in routes for method in route.methods}) == 49


def test_mysql_schema_matches_pre_refactor_contract():
    expected = json.loads((FIXTURES / "learning_mysql_schema.json").read_text(encoding="utf-8"))

    assert mysql_schema_contract() == expected


def test_multiple_apps_keep_auth_materials_and_runs_isolated():
    first_app = isolated_app(token="first-app-token")
    second_app = isolated_app(token="second-app-token")
    first_headers = {"X-Local-Token": "first-app-token"}
    second_headers = {"X-Local-Token": "second-app-token"}

    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get("/api/v1/materials", headers=second_headers).status_code == 401
        assert second.get("/api/v1/materials", headers=first_headers).status_code == 401
        created_ids = []
        for client, headers, content in (
            (first, first_headers, b"First application"),
            (second, second_headers, b"Second application"),
        ):
            response = client.post(
                "/api/v1/materials",
                files={"file": ("notes.txt", content, "text/plain")},
                data={"auto_ingest": "false"},
                headers={**headers, "Idempotency-Key": "shared-key"},
            )
            assert response.status_code == 201
            created_ids.append(response.json()["material"]["id"])
        assert created_ids[0] != created_ids[1]
        for client, headers, own_id, other_id in (
            (first, first_headers, created_ids[0], created_ids[1]),
            (second, second_headers, created_ids[1], created_ids[0]),
        ):
            response = client.get("/api/v1/materials", headers=headers)
            assert response.status_code == 200
            assert [item["id"] for item in response.json()["items"]] == [own_id]
            assert client.get(f"/api/v1/materials/{other_id}", headers=headers).status_code == 404

        first_run = first_app.state.learning_state.run("first", status="running")
        second_run = second_app.state.learning_state.run("second", status="running")
        assert first.get(f"/api/v1/runs/{second_run['id']}", headers=first_headers).status_code == 404
        assert second.get(f"/api/v1/runs/{first_run['id']}", headers=second_headers).status_code == 404
        cancelled = first.post(
            f"/api/v1/runs/{first_run['id']}/cancel", json={}, headers=first_headers,
        )
        assert cancelled.status_code == 202
        assert second.get(f"/api/v1/runs/{second_run['id']}", headers=second_headers).json()["status"] == "running"
