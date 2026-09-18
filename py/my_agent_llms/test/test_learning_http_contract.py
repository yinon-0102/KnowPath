"""Local HTTP boundary: origin, token, unknown fields and safe correlated errors."""
import pytest
from fastapi.testclient import TestClient

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.config import LearningSettings


def test_origin_whitelist_applies_before_writes():
    app = create_app(settings=LearningSettings(local_token="test-local-token"))
    with TestClient(app) as client:
        denied = client.get("/api/v1/materials", headers={"X-Local-Token": "test-local-token", "Origin": "https://untrusted.example"})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
        accepted = client.get("/api/v1/materials", headers={"X-Local-Token": "test-local-token", "Origin": "http://localhost:5173"})
        assert accepted.status_code == 200
        assert accepted.headers["access-control-allow-origin"] == "http://localhost:5173"
        preflight = client.options("/api/v1/materials", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-Local-Token,Idempotency-Key,Content-Type"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert client.get("/api/v1/materials?token=test-local-token").status_code == 401


def test_public_health_is_minimal_when_authentication_enabled():
    with TestClient(create_app(settings=LearningSettings(local_token="test-local-token"))) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert set(health.json()) == {"status"}
        assert client.get("/api/v1/materials").status_code == 401
        assert client.get("/openapi.json").status_code == 401


def test_every_error_has_a_request_id_and_does_not_echo_internal_failure():
    app = create_app()
    @app.get("/api/v1/test-failure")
    def broken():
        raise RuntimeError("private-provider-key-and-connection-string")
    with TestClient(app, raise_server_exceptions=False) as client:
        for path, expected in [("/api/v1/materials?limit=0", 422), ("/api/v1/materials/missing", 404), ("/api/v1/test-failure", 500), ("/api/v1/missing-route", 404)]:
            response = client.get(path)
            assert response.status_code == expected
            error = response.json()["error"]
            assert error["request_id"] == response.headers["X-Request-ID"]
            assert "private-provider" not in response.text


@pytest.mark.parametrize("path", ["/api/v1/materials", "/api/v1/learning-spaces", "/api/v1/health"])
def test_unknown_query_fields_are_rejected(path):
    with TestClient(create_app()) as client:
        assert client.get(path, params={"unexpected": "ignored-before"}).status_code == 422


def test_unknown_multipart_fields_and_cancel_fields_are_rejected():
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/v1/materials", files={"file": ("notes.md", b"# Topic\n\nContent")}, data={"auto_ingest": "false", "unexpected": "ignored-before"}, headers={"Idempotency-Key": "upload"})
        assert response.status_code == 422
        run = app.state.learning_state.run_service.create("test", status="running")
        assert client.post(f"/api/v1/runs/{run['id']}/cancel", json={"unexpected": True}).status_code == 422
        assert app.state.learning_state.get_run(run["id"])["status"] == "running"


def test_server_token_is_generated_once_without_printing(tmp_path, monkeypatch, capsys):
    from my_agent_llms.learning.config import server_settings
    monkeypatch.delenv("LEARNING_LOCAL_TOKEN", raising=False)
    path = tmp_path / "session-token.local"
    monkeypatch.setenv("LEARNING_LOCAL_TOKEN_FILE", str(path))
    first, second = server_settings(), server_settings()
    assert first.local_token == second.local_token == path.read_text().strip()
    assert len(first.local_token) >= 32
    assert first.local_token not in capsys.readouterr().out


def test_concurrent_server_startups_share_a_complete_token(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from my_agent_llms.learning.config import server_settings
    monkeypatch.delenv("LEARNING_LOCAL_TOKEN", raising=False)
    path = tmp_path / "concurrent-token.local"
    monkeypatch.setenv("LEARNING_LOCAL_TOKEN_FILE", str(path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: server_settings().local_token, range(32)))
    assert len(set(tokens)) == 1
    assert tokens[0] == path.read_text()
