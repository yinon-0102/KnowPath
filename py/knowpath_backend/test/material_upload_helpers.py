"""Explicit ready-material fixtures for domain tests, using the real durable workers.

Upload contract tests use plain TestClient. This helper leaves the HTTP response
unchanged and finishes only the Run belonging to the requested upload, so it is
safe when integration tests share a database with other jobs.
"""
from fastapi.testclient import TestClient

from knowpath_backend.learning.workers.graph import GraphWorker
from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
from knowpath_backend.test.test_learning_graph_worker import PreparedBackend


def complete_upload(client, response):
    if response.status_code != 201:
        return response
    data = response.json()
    run_id = data.get("run_id")
    if run_id is None:
        return response
    state = client.app.state.learning_state
    graph = state.graph_service
    for kind, worker in (("material.parse", MaterialParseWorker(graph)),
                         ("graph.prepare", GraphWorker(graph, PreparedBackend()))):
        for event in graph.repository.records("outbox", event_type=kind):
            if event["payload"].get("run_id") == run_id and event["status"] == "pending":
                assert worker.run_once(event["id"])
    assert state.get_run(run_id)["status"] == "succeeded"
    material_id = data["material"]["id"] if "material" in data else data["material_id"]
    if graph._published(graph._history(material_id)) is None:
        candidates = [r for r in graph._history(material_id) if r["run_id"] == run_id]
        if candidates:
            graph.publish(material_id, candidates[-1]["id"], {"expected_graph_version": 0, "resolutions": []}, None)
    return response


class ParsedUploadClient(TestClient):
    """Domain fixture client that waits for uploads before subsequent requests."""
    def post(self, url, **kwargs):
        response = super().post(url, **kwargs)
        if kwargs.get("files") and str(url).startswith("/api/v1/materials"):
            complete_upload(self, response)
        return response
