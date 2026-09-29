"""Erasing a selected material invalidates its frozen selection metadata."""
import pytest
from knowpath_backend.test.test_learning_material_deletion import workspace, seed, delete
from knowpath_backend.test.test_learning_messages import FixedAnswer


@pytest.mark.parametrize('completed', [False, True])
def test_material_erasure_does_not_retain_request_scope_metadata(workspace, completed):
    state = workspace()
    first, _, space = seed(state)
    state.message_service.generator = FixedAnswer()
    job = state.send_message(space['id'], {'message': 'Explain', 'material_ids': [first.id]},
        dispatch=None if completed else lambda *args: None)
    repo = state.message_service.repository
    message = repo.records('messages', space_id=space['id'])[0]
    assert first.id in message['snapshot']['request_scope']['material_ids']
    delete(state, first.id)
    message = repo.get_record('messages', message['id'])
    assert 'request_scope' not in message['snapshot']
    assert message['snapshot']['request_scope_invalidated'] is True
    if completed:
        assert 'request_scope' not in message['response']
    else:
        assert state.get_run(job['run_id'])['status'] == 'cancelled'
