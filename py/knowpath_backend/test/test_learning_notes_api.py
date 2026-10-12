"""Notebook contracts are authenticated, strict and optimistic."""
from fastapi.testclient import TestClient
from knowpath_backend.learning.api import create_app
from knowpath_backend.test.test_learning_workbench import workbench
from knowpath_backend.learning.config import LearningSettings


def client_for(state):
    app = create_app()
    app.state.learning_state = state
    return TestClient(app)


def test_notes_index_and_edit_contract(workbench):
    _, state, space_id, _, _ = workbench
    state.create_plan(space_id, {})
    client = client_for(state)
    book = client.get(f'/api/v1/learning-spaces/{space_id}/notebook')
    assert book.status_code == 200
    cid = book.json()['chapters'][0]['id']
    chapter = client.get(f'/api/v1/note-chapters/{cid}').json()
    payload = {'expected_version': chapter['version'], 'blocks': [{'markdown': '个人补充'}]}
    edited = client.patch(f'/api/v1/note-chapters/{cid}', json=payload)
    assert edited.status_code == 200
    assert edited.json()['blocks'][0]['kind'] == 'personal'
    conflict = client.patch(f'/api/v1/note-chapters/{cid}', json=payload)
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'VERSION_CONFLICT'
    invalid = client.patch(f'/api/v1/note-chapters/{cid}', json={**payload, 'status': 'COMPLETED'})
    assert invalid.status_code == 422
    revisions = client.get(f'/api/v1/note-chapters/{cid}/revisions')
    assert revisions.status_code == 200
    assert client.get('/api/v1/notebooks').status_code == 200


def test_unknown_notes_use_existing_error_contract(workbench):
    _, state, _, _, _ = workbench
    client = client_for(state)
    for path in ['/note-chapters/missing', '/learning-spaces/missing/notebook',
                 '/note-chapters/missing/revisions', '/note-chapters/missing/revisions/missing']:
        response = client.get('/api/v1' + path)
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert client.post('/api/v1/note-generations/missing/retry', json={}).status_code == 400
    assert client.post('/api/v1/note-generations/missing/retry', json={},
                       headers={'Idempotency-Key': 'retry-missing'}).status_code == 404


def test_note_provider_configuration_failure_does_not_break_application():
    app = create_app(settings=LearningSettings(chat_provider='missing-provider'))
    assert TestClient(app).get('/api/v1/health').status_code == 200


def test_notes_reuse_local_authentication():
    app = create_app(settings=LearningSettings(local_token='test-notes-token'))
    client = TestClient(app)
    assert client.get('/api/v1/notebooks').status_code == 401
    assert client.get('/api/v1/notebooks', headers={'X-Local-Token': 'test-notes-token'}).status_code == 200
