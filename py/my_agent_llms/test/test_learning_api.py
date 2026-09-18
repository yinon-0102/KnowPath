from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from my_agent_llms.learning.api import create_app


def test_health_exposes_learning_runtime_and_embedding_default():
    client = TestClient(create_app())

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == "keel-learning"
    assert body["dependencies"]["embedding"]["provider"] == "dashscope"
    assert body["dependencies"]["embedding"]["model"] == "text-embedding-v3"


def test_material_upload_requires_idempotency_key_and_replays_same_resource():
    client = TestClient(create_app())
    payload = {"file": ("functions.md", b"# Functions\n\nA reusable block.", "text/markdown")}

    missing = client.post("/api/v1/materials", files=payload)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    first = client.post(
        "/api/v1/materials",
        files=payload,
        headers={"Idempotency-Key": "upload-1"},
    )
    retry = client.post(
        "/api/v1/materials",
        files=payload,
        headers={"Idempotency-Key": "upload-1"},
    )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["material"]["id"] == first.json()["material"]["id"]
    assert retry.json()["run_id"] == first.json()["run_id"]
    assert retry.json()["replayed"] is True


def test_material_upload_rejects_key_reuse_with_different_content():
    client = TestClient(create_app())
    headers = {"Idempotency-Key": "upload-1"}

    client.post(
        "/api/v1/materials",
        files={"file": ("a.txt", b"a", "text/plain")},
        headers=headers,
    )
    response = client.post(
        "/api/v1/materials",
        files={"file": ("b.txt", b"b", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_material_version_and_source_chunk_are_queryable():
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/materials",
        files={"file": ("functions.md", b"# Functions\n\nReusable behavior.", "text/markdown")},
        headers={"Idempotency-Key": "upload-1"},
    )
    material_id = created.json()["material"]["id"]
    version_id = created.json()["version"]["id"]

    versions = client.get(f"/api/v1/materials/{material_id}/versions")
    assert versions.status_code == 200
    assert versions.json()["items"][0]["id"] == version_id

    material = client.get(f"/api/v1/materials/{material_id}").json()
    chunk_id = material["version"]["chunks"][0]["id"]
    chunk = client.get(
        f"/api/v1/materials/{material_id}/versions/{version_id}/chunks/{chunk_id}"
    )
    assert chunk.status_code == 200
    assert chunk.json()["text"] == "Reusable behavior."


def test_run_events_support_last_event_id_and_include_timestamps():
    app = create_app()
    client = TestClient(app)
    run = app.state.learning_state.run("demo")

    response = client.get(f"/api/v1/runs/{run['id']}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 1" in response.text
    assert "event: run.started" in response.text
    assert "timestamp" in response.text

    resumed = client.get(
        f"/api/v1/runs/{run['id']}/events",
        headers={"Last-Event-ID": "1"},
    )
    assert resumed.status_code == 200
    assert "id: 1" not in resumed.text
    assert "id: 2" in resumed.text


def test_run_events_reject_expired_history(monkeypatch):
    app = create_app()
    client = TestClient(app)
    run = app.state.learning_state.run("demo")
    monkeypatch.setattr(app.state.learning_state.run_service, "clock",
                        lambda: datetime.now(timezone.utc) + timedelta(days=8))

    response = client.get(f"/api/v1/runs/{run['id']}/events")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "EVENT_HISTORY_EXPIRED"
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "succeeded"


def test_cancel_run_waits_for_acknowledgement_and_is_terminal_idempotent():
    app = create_app()
    client = TestClient(app)
    run = app.state.learning_state.run("demo", status="running")

    first = client.post(f"/api/v1/runs/{run['id']}/cancel", json={})
    second = client.post(f"/api/v1/runs/{run['id']}/cancel", json={})

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"id": run["id"], "status": "cancelling"}
    assert app.state.learning_state.get_run(run["id"])["finished_at"] is None
    app.state.learning_state.run_service.acknowledge_cancel(run["id"])
    third = client.post(f"/api/v1/runs/{run['id']}/cancel", json={})
    assert third.status_code == 200
    assert third.json()["status"] == "cancelled"
    events = app.state.learning_state.get_run(run["id"])["events"]
    assert [event["event"] for event in events][-1] == "run.cancelled"
    assert sum(event["event"] == "run.cancelled" for event in events) == 1


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.0", "999", "9" * 100])
def test_run_events_reject_invalid_or_future_cursor(cursor):
    app = create_app()
    run = app.state.learning_state.run("demo")
    response = TestClient(app).get(
        f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": cursor}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_EVENT_ID"


def test_terminal_cursor_closes_stream_and_run_response_omits_events():
    app = create_app()
    run = app.state.learning_state.run("demo")
    client = TestClient(app)
    response = client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "2"})
    assert response.status_code == 200
    assert response.text == ""
    assert response.headers["cache-control"] == "no-cache"
    assert "events" not in client.get(f"/api/v1/runs/{run['id']}").json()
    assert client.get("/api/v1/runs/missing/events").status_code == 404
