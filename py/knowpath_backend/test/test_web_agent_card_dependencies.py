"""A consumed L0 card is a card reference, not a message item reference."""
from knowpath_backend.test.test_learning_state_persistence import workspace


def test_stored_card_dependency_rechecks_lifecycle_after_restart(workspace):
    from knowpath_backend.memory.playbook.card import PlaybookCard
    factory, sid, _, _ = workspace
    state = factory()
    state.send_message(sid, {'message': 'Explain functions'})
    service = state.message_service
    message = service.repository.records('messages', space_id=sid)[0]
    repo = service.repository.agent_memory_factory(sid)
    source = repo.items_for_turn(message['id'])[0]
    card = PlaybookCard(content='Functions are reusable', type='state', source='llm_remembered', metadata=source.metadata)
    with repo.transaction():
        repo.put_card(card)
    restarted = factory()
    service = restarted.message_service
    repo = service.repository.agent_memory_factory(sid)
    policy = service._build_memory_policy(repo, message['snapshot']['memory_scope'], restarted.material_repository)
    candidate = source.model_copy(deep=True)
    candidate.metadata.pop('source_ref', None)
    candidate.metadata['source_dependencies'] = [{'kind': 'card', 'id': card.id}]
    assert policy.eligible(candidate, purpose='recall')
    with repo.transaction():
        card.forget()
        repo.put_card(card)
    assert not policy.eligible(candidate, purpose='recall')
