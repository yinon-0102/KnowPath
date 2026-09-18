"""Published graph reads survive restarts and honor traversal/source bounds."""
import pytest
from fastapi.testclient import TestClient
from my_agent_llms.learning.api import create_app
from my_agent_llms.test.test_learning_graph_scope_contract import workspace
from my_agent_llms.test.test_learning_graph_worker import worker, event_id

@pytest.fixture
def graph_client():
    state, upload = workspace("# Root\n\n## A\n\nA fact.\n\n## B\n\nPrerequisites: A\n\n# C\n\nPrerequisites: B")
    staged = state.graph_service.reconcile(upload.material.id, {"version_id": upload.version.id, "expected_graph_version": 0}, "reconcile")
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged["candidate_revision_id"], {"expected_graph_version": 0, "resolutions": []}, "publish")
    app = create_app(state.material_service)
    with TestClient(app) as client:
        yield client, upload, staged, app

def test_published_topics_expose_versioned_tree_and_depth(graph_client):
    client, upload, staged, app = graph_client
    response = client.get(f"/api/v1/materials/{upload.material.id}/topics", params={"depth": 1})
    assert response.status_code == 200
    result = response.json()
    assert result["graph_version"] == 1
    assert {n["name"] for n in result["items"]} == {"Root", "C"}
    assert all(n["revision_id"] == staged["candidate_revision_id"] for n in result["items"])
    publication = app.state.learning_state.graph_service.repository.get_record("graph_revisions", staged["candidate_revision_id"])["publication"]
    assert all(n["valid_from"] == publication["published_at"] and n["valid_from"] for n in result["items"])

def test_graph_reads_persisted_edges_without_space_or_cache(graph_client):
    client, upload, _, app = graph_client
    topics = client.get(f"/api/v1/materials/{upload.material.id}/topics").json()["items"]
    ids = {n["name"]: n["id"] for n in topics}
    app.state.learning_state.topics.clear()
    one = client.get(f"/api/v1/topics/{ids['A']}/graph", params={"depth": 1}).json()
    assert {n["name"] for n in one["nodes"]} == {"A", "B", "Root"}
    assert {e["type"] for e in one["edges"]} == {"contains", "prerequisite_of"}
    assert one["sources"] and one["graph_version"] == 1
    two = client.get(f"/api/v1/topics/{ids['A']}/graph", params={"depth": 2, "include_sources": False}).json()
    assert {n["name"] for n in two["nodes"]} == {"A", "B", "C", "Root"}
    assert "sources" not in two
    assert all("source_refs" not in n for n in two["nodes"] + two["edges"])
    assert client.get(f"/api/v1/topics/{ids['A']}/graph", params={"depth": 4}).status_code == 422

def test_topic_graph_unknown_is_not_found(graph_client):
    client, _, _, _ = graph_client
    assert client.get("/api/v1/topics/missing/graph").status_code == 404
