"""Request document boundaries apply before retrieval and contextual generation."""
import copy

import pytest
from pydantic import ValidationError

from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.conversations.schemas import SendMessage
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.test.test_learning_messages import FixedAnswer


@pytest.fixture
def documents():
    state = LearningState(InMemoryMaterialRepository())
    uploads = [state.material_service.create(filename=f'{name}.md',
        content=f'# {name}\n\nFunctions group reusable behavior. Secret from {name}.'.encode(),
        idempotency_key=name) for name in ('Alpha', 'Beta', 'Foreign')]
    space = state.create_space({'name': 'Documents', 'material_ids': [u.material.id for u in uploads[:2]]})
    generator = FixedAnswer()
    state.message_service.generator = generator
    return state, space, uploads, generator


def test_contract_canonicalizes_selection_and_keeps_default():
    assert SendMessage(message='q', material_ids=['b', 'a', 'b']).material_ids == ['a', 'b']
    assert SendMessage(message='q').material_ids is None
    assert SendMessage(message='q', material_ids=None).material_ids is None


@pytest.mark.parametrize('selection', [[], [''], [' '], [1], ['a'] * 6])
def test_contract_rejects_invalid_selection(selection):
    with pytest.raises(ValidationError):
        SendMessage(message='q', material_ids=selection)


def test_single_document_filters_actual_retriever_and_generator_input(documents):
    state, space, uploads, generator = documents
    selected = uploads[0].material.id
    calls = []
    class Retriever:
        def select(self, message, sources, **kwargs):
            calls.append(copy.deepcopy(sources))
            return sources
    state.message_service.retriever = Retriever()
    result = state.send_message(space['id'], {'message': 'Explain', 'material_ids': [selected]})
    assert state.get_run(result['run_id'])['status'] == 'succeeded'
    assert {r['material_id'] for r in calls[0]} == {selected}
    assert {r['material_id'] for r in generator.calls[0]['sources']} == {selected}
    scope = generator.calls[0]['request_scope']
    assert scope['mode'] == 'selected' and scope['material_ids'] == [selected]
    assert scope['request_scope_id']


def test_foreign_document_rejected_before_dispatch(documents):
    state, space, uploads, generator = documents
    jobs = []
    with pytest.raises(DomainConflict) as error:
        state.send_message(space['id'], {'message': 'q', 'material_ids': [uploads[2].material.id]},
            dispatch=lambda *args: jobs.append(args))
    assert error.value.code == 'MATERIAL_OUT_OF_SCOPE'
    assert jobs == [] and generator.calls == []


def test_selection_is_frozen_and_part_of_idempotency(documents):
    state, space, uploads, generator = documents
    ids = [u.material.id for u in uploads[:2]]
    jobs = []
    body = {'message': 'q', 'material_ids': ids.copy()}
    first = state.send_message(space['id'], body, idempotency_key='selection', dispatch=lambda *a: jobs.append(a))
    body['material_ids'].clear()
    assert state.send_message(space['id'], {'message': 'q', 'material_ids': ids[::-1]}, idempotency_key='selection') == first
    with pytest.raises(DomainConflict) as error:
        state.send_message(space['id'], {'message': 'q', 'material_ids': ids[:1]}, idempotency_key='selection')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    jobs[0][0](jobs[0][1])
    assert set(generator.calls[0]['request_scope']['material_ids']) == set(ids)


def test_empty_effective_scope_does_not_expand_to_other_document(documents):
    state, space, uploads, generator = documents
    topic = next(t for t in state.space_service.bound_topics(space) if t['source_refs'][0]['material_id'] == uploads[1].material.id)
    state.set_scope(space['id'], {'topic_ids': [topic['id']], 'expected_version': 1})
    with pytest.raises(DomainConflict) as error:
        state.send_message(space['id'], {'message': 'q', 'material_ids': [uploads[0].material.id]})
    assert error.value.code == 'NO_LEARNING_SOURCES'
    assert not generator.calls
