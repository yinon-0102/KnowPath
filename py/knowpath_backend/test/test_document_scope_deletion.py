"""Erasing a selected material invalidates its frozen selection metadata."""
import pytest
from knowpath_backend.test.test_learning_material_deletion import workspace, seed, delete
from knowpath_backend.test.test_learning_messages import FixedAnswer


@pytest.fixture(autouse=True)
def isolated_scope_erasure_retrieval(monkeypatch):
    # These source-erasure fixtures use local chunks, not a prepared vector index.
    monkeypatch.setenv('LEARNING_RETRIEVAL_BACKEND', 'keyword')
    monkeypatch.setenv('LEARNING_RAG_PLUGIN', 'legacy')


@pytest.mark.parametrize('completed', [False, True])
def test_material_erasure_does_not_retain_request_scope_metadata(workspace, completed):
    state = workspace()
    first, _, space = seed(state)
    state.message_service.generator = FixedAnswer()
    job = state.send_message(space['id'], {'message': 'Explain', 'material_ids': [first.id]},
        dispatch=None if completed else lambda *args: None)
    repo = state.message_service.repository
    message = repo.records('messages', space_id=space['id'])[0]
    if completed:
        assert message['status'] == 'completed' and message['response'] is not None
    assert first.id in message['snapshot']['request_scope']['material_ids']
    delete(state, first.id)
    message = repo.get_record('messages', message['id'])
    assert 'request_scope' not in message['snapshot']
    assert message['snapshot']['request_scope_invalidated'] is True
    if completed:
        assert 'request_scope' not in message['response']
    else:
        assert state.get_run(job['run_id'])['status'] == 'cancelled'


@pytest.mark.parametrize('completed', [False, True])
def test_material_erasure_removes_frozen_reading_context(workspace, completed):
    state = workspace()
    first, _, space = seed(state)
    binding = next(b for b in space['bindings'] if b['material_id'] == first.id)
    context = {k: binding[k] for k in ('material_id', 'material_version_id')}
    state.message_service.generator = FixedAnswer()
    sent = state.send_message(space['id'], {'message': 'Explain', 'reading_context': context},
        dispatch=None if completed else lambda *args: None)
    message = state.message_service.repository.records('messages', run_id=sent['run_id'])[0]
    if completed:
        assert message['status'] == 'completed' and message['response'] is not None
    assert message['snapshot']['reading_context'] == context
    delete(state, first.id)
    message = state.message_service.repository.get_record('messages', message['id'])
    assert 'reading_context' not in message['snapshot']
    if completed:
        from knowpath_backend.learning.errors import DomainConflict
        with pytest.raises(DomainConflict) as exc:
            state.message_service.history(space['id'], sent['session_id'])
        assert exc.value.code == 'BOUND_VERSION_UNAVAILABLE'
    else:
        assert state.message_service.history(space['id'], sent['session_id'])['items'][0]['reading_context'] is None


def test_reading_material_erasure_during_stream_fences_late_answer(workspace):
    state = workspace()
    first, _, space = seed(state)
    binding = next(b for b in space['bindings'] if b['material_id'] == first.id)
    context = {k: binding[k] for k in ('material_id', 'material_version_id')}
    class DeleteDuringStream:
        def stream(self, snapshot, on_text):
            on_text('erased draft')
            delete(state, first.id)
            on_text('late erased draft' * 20)
            return {'text': 'late erased answer', 'citation_ids': [snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator = DeleteDuringStream()
    sent = state.send_message(space['id'], {'message': 'Explain original', 'reading_context': context})
    run = state.run_service.get(sent['run_id'])
    assert run['status'] == 'cancelled'
    assert not any(e['event'] == 'message.delta' and e['data'].get('delta') for e in run['events'])
    assert not any(e['event'] == 'message.completed' for e in run['events'])
    row = state.message_service.repository.records('messages', run_id=sent['run_id'])[0]
    assert row['response'] is None and 'reading_context' not in row['snapshot']
