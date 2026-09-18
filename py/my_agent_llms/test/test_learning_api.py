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
