from fastapi.testclient import TestClient
from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.materials import MaterialService, InMemoryMaterialRepository


def test_http_space_requires_published_snapshot_even_when_parsed():
    service = MaterialService(InMemoryMaterialRepository())
    upload = service.create(filename="notes.md", content=b"# Topic\n\nA fact.", idempotency_key="upload")
    with TestClient(create_app(service)) as client:
        response = client.post("/api/v1/learning-spaces", json={"material_ids": [upload.material.id]}, headers={"Idempotency-Key": "space"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MATERIAL_NOT_READY"
        too_many = client.post("/api/v1/learning-spaces", json={"material_ids": [str(i) for i in range(6)]}, headers={"Idempotency-Key": "many"})
        assert too_many.status_code == 422
