"""A prior epoch may be recalled without becoming adjacent conversation."""
from copy import deepcopy

from knowpath_backend.test.test_learning_state_persistence import workspace


def test_old_epoch_is_recallable_but_never_adjacent(workspace):
    from knowpath_backend.learning.conversations.context import project_memory
    factory, sid, _, _ = workspace
    state = factory()
    state.send_message(sid, {'message': 'Explain functions'})
    service = state.message_service
    rows = service.repository.records('messages', space_id=sid)
    original = rows[0]
    current = deepcopy(original['snapshot']['memory_scope'])
    current['context_epoch'] += 2
    current['receive_sequence'] += 2
    repo = service.repository.agent_memory_factory(sid)
    policy = service._build_memory_policy(repo, current, state.material_repository)
    result = project_memory(rows, state.space_service.repository.get(sid),
        original['conversation_id'], 'Explain functions',
        request_scope=original['snapshot']['request_scope'], eligibility_policy=policy)
    assert result['history'] == []
    assert result['memory']['summary'] == {}
    assert [row['message_id'] for row in result['memory']['recall']] == [original['id']]
