from knowpath_backend.test.test_message_document_scope import documents
from knowpath_backend.learning.conversations.context import project_memory
from knowpath_backend.learning.conversations.document_scope import resolve_document_scope


def test_history_recall_and_summary_do_not_cross_document_selection(documents):
    state, space, uploads, generator = documents
    a, b = [u.material.id for u in uploads[:2]]
    first = state.send_message(space['id'], {'message': 'Explain Beta', 'material_ids': [b]})
    state.send_message(space['id'], {'message': 'Explain Alpha', 'session_id': first['session_id'], 'material_ids': [a]})
    current = generator.calls[-1]
    assert current['history'] == []
    assert current['memory'] == {'summary': {}, 'recall': []}
    state.send_message(space['id'], {'message': 'More Alpha', 'session_id': first['session_id'], 'material_ids': [a]})
    assert len(generator.calls[-1]['history']) == 2
    assert generator.calls[-1]['history'][0]['content'] == 'Explain Alpha'


def test_scope_switch_cannot_resolve_pronouns_from_other_document(documents):
    state, space, uploads, generator = documents
    a, b = [u.material.id for u in uploads[:2]]
    first = state.send_message(space['id'], {'message': 'Explain Beta', 'material_ids': [b]})
    job = state.send_message(space['id'], {'message': '继续', 'session_id': first['session_id'], 'material_ids': [a]})
    assert len(generator.calls) == 1
    response = next(e['data'] for e in state.events_for(job['run_id']) if e['event'] == 'message.completed')
    assert response['answer_status'] == 'clarify'
    assert response['citations'] == []


def test_long_term_recall_and_summary_cannot_reintroduce_other_document(documents):
    state, space, uploads, generator = documents
    a, b = [u.material.id for u in uploads[:2]]
    first = state.send_message(space['id'], {'message': 'Explain Beta', 'material_ids': [b]})
    for i in range(14):
        state.send_message(space['id'], {'message': f'Explain Beta {i}', 'session_id': first['session_id'], 'material_ids': [b]})
    state.send_message(space['id'], {'message': 'Explain Alpha', 'session_id': first['session_id'], 'material_ids': [a]})
    snapshot = generator.calls[-1]
    assert snapshot['history'] == []
    assert snapshot['memory'] == {'summary': {}, 'recall': []}
    assert snapshot['context_provenance'] == {'history': [], 'summary': [], 'recall': {}}


def test_old_messages_remain_usable_only_for_default_selection(documents):
    state, space, uploads, _ = documents
    first = state.send_message(space['id'], {'message': 'Explain all'})
    repo = state.message_service.repository
    rows = repo.records('messages', space_id=space['id'])
    rows[0]['snapshot'].pop('request_scope')
    all_scope = resolve_document_scope(space)
    selected_scope = resolve_document_scope(space, [uploads[0].material.id])
    assert project_memory(rows, space, first['session_id'], 'q', request_scope=all_scope)['history']
    assert project_memory(rows, space, first['session_id'], 'q', request_scope=selected_scope)['history'] == []


def test_explicit_all_and_default_share_effective_scope(documents):
    state, space, uploads, generator = documents
    ids = [u.material.id for u in uploads[:2]]
    first = state.send_message(space['id'], {'message': 'Explain all', 'material_ids': ids})
    state.send_message(space['id'], {'message': 'Another question', 'session_id': first['session_id']})
    assert len(generator.calls[-1]['history']) == 2


