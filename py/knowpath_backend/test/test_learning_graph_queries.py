"""Published graph reads survive restarts and honor traversal/source bounds."""
import copy
import pytest
from fastapi.testclient import TestClient
from knowpath_backend.learning.api import create_app
from knowpath_backend.test.test_learning_graph_scope_contract import workspace
from knowpath_backend.test.test_learning_graph_worker import worker, event_id
from knowpath_backend.test.test_learning_graph_reconciliation import graph_workspace

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

def test_material_graph_returns_complete_projection_in_one_read(graph_client):
    client, upload, staged, _ = graph_client
    response = client.get(f"/api/v1/materials/{upload.material.id}/graph",
                          params={"version_id": upload.version.id, "include_sources": True})
    assert response.status_code == 200
    result = response.json()
    assert result["material_id"] == upload.material.id
    assert result["version_id"] == upload.version.id
    assert result["graph_version"] == 1
    assert {node["name"] for node in result["nodes"]} == {"Root", "A", "B", "C"}
    assert {edge["type"] for edge in result["edges"]} == {"contains", "prerequisite_of"}
    assert result["sources"]

def test_topic_graph_unknown_is_not_found(graph_client):
    client, _, _, _ = graph_client
    assert client.get("/api/v1/topics/missing/graph").status_code == 404


@pytest.mark.parametrize('projection,items', [('topics', 'items'), ('graph', 'nodes')])
def test_same_material_version_reads_exact_published_revision(graph_client, projection, items):
    client, upload, staged, app = graph_client
    repo = app.state.learning_state.graph_service.repository
    first = repo.get_record('graph_revisions', staged['candidate_revision_id'])
    original = copy.deepcopy(first['publication']['snapshot'])
    first['status'] = 'superseded'
    repo.put_record('graph_revisions', first)
    second = copy.deepcopy(first)
    second.update(id='second-revision', sequence=2, graph_version=2, status='published')
    second['publication']['snapshot']['nodes'][0]['name'] = 'Updated topic'
    repo.put_record('graph_revisions', second)
    path = f'/api/v1/materials/{upload.material.id}/{projection}'
    params = {'version_id': upload.version.id, 'graph_revision_id': first['id']}
    pinned = client.get(path, params=params)
    assert pinned.status_code == 200
    assert pinned.json()['graph_revision_id'] == first['id']
    assert pinned.json()['graph_version'] == 1
    assert [n['name'] for n in pinned.json()[items]] == [n['name'] for n in original['nodes']]
    legacy = client.get(path, params={'version_id': upload.version.id, 'graph_version': 1}).json()
    assert legacy['graph_revision_id'] == first['id']
    latest = client.get(path, params={'version_id': upload.version.id}).json()
    assert latest['graph_revision_id'] == second['id'] and latest['graph_version'] == 2
    assert repo.get_record('graph_revisions', first['id'])['publication']['snapshot'] == original


@pytest.mark.parametrize('projection', ['topics', 'graph'])
@pytest.mark.parametrize('invalid', ['missing', 'foreign_material', 'foreign_version', 'draft', 'no_publication', 'foreign_publication', 'deleted_version', 'wrong_graph_version'])
def test_explicit_revision_never_falls_back_to_latest(graph_client, projection, invalid, monkeypatch):
    client, upload, staged, app = graph_client
    state = app.state.learning_state
    repo = state.graph_service.repository
    row = repo.get_record('graph_revisions', staged['candidate_revision_id'])
    params = {'version_id': upload.version.id, 'graph_revision_id': row['id']}
    if invalid == 'missing':
        params['graph_revision_id'] = 'missing'
    elif invalid == 'foreign_material':
        row['material_id'] = 'other'
    elif invalid == 'foreign_version':
        row['material_version_id'] = 'other'
    elif invalid == 'draft':
        row['status'] = 'draft'
    elif invalid == 'no_publication':
        row.pop('publication')
    elif invalid == 'foreign_publication':
        row['publication']['snapshot']['material_id'] = 'other'
    elif invalid == 'deleted_version':
        monkeypatch.setattr(state.graph_service.materials, 'get_version', lambda _: None)
    elif invalid == 'wrong_graph_version':
        params['graph_version'] = 99
    repo.put_record('graph_revisions', row)
    response = client.get(f'/api/v1/materials/{upload.material.id}/{projection}', params=params)
    assert response.status_code == 404
    assert response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'


@pytest.mark.parametrize('projection,items', [('topics', 'items'), ('graph', 'nodes')])
def test_legacy_binding_keeps_original_version_projection_after_publication(graph_workspace, projection, items):
    factory, upload, _ = graph_workspace
    state = factory()
    space = state.create_space({'material_ids': [upload.material.id]})
    assert 'graph_revision_id' not in space['bindings'][0]
    expected = state.space_service.bound_topics(space)
    canonical = [n for n in expected if n['id'] == n['canonical_topic_id']]
    path = f'/api/v1/materials/{upload.material.id}/{projection}'
    params = {'version_id': upload.version.id, 'graph_version': 1, 'legacy_snapshot': True}
    with TestClient(create_app(state.material_service)) as client:
        for published_version in (0, 1, 2):
            if published_version:
                from knowpath_backend.learning.knowledge.reconciliation import extract_snapshot
                snapshot = extract_snapshot(upload.material.id, upload.version)
                snapshot['nodes'][0].update(id=f'published-{published_version}', name='Published replacement')
                state.graph_service.repository.put_record('graph_revisions', {
                    'id': f'revision-{published_version}', 'material_id': upload.material.id,
                    'material_version_id': upload.version.id, 'sequence': published_version,
                    'base_graph_version': published_version - 1, 'run_id': 'fixture-run',
                    'snapshot': snapshot, 'snapshot_hash': 'fixture', 'diff': {},
                    'created_at': upload.version.created_at.isoformat(),
                    'graph_version': published_version, 'status': 'published', 'publication': {'snapshot': snapshot}})
            response = client.get(path, params=params)
            assert response.status_code == 200
            result = response.json()
            assert result['graph_revision_id'] is None and result['graph_version'] == 1
            assert [(n['id'], n['name'], n['source_refs']) for n in result[items]] == [
                (n['id'], n['name'], n['source_refs']) for n in canonical]
        for invalid in ({'graph_version': 2}, {'graph_revision_id': 'revision-1'}, {'version_id': None}):
            assert client.get(path, params={k: v for k, v in (params | invalid).items() if v is not None}).status_code == 404
        latest = client.get(path, params={'version_id': upload.version.id}).json()
        assert latest['graph_version'] == 2 and latest['graph_revision_id'] == 'revision-2'
