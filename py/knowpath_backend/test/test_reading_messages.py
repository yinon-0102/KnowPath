"""Original-document conversations are version-bound and context-isolated."""
import copy
from dataclasses import replace
import hashlib

import pytest
from pydantic import ValidationError

from knowpath_backend.learning.conversations.schemas import SendMessage
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository, MaterialParser
from knowpath_backend.learning.rag.retrieval import KeywordRetriever
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.test.test_learning_messages import FixedAnswer, service
from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_study_lessons import setup_lesson, Generator


def reading(space, index=0):
    return {k: space['bindings'][index][k] for k in ('material_id', 'material_version_id')}


@pytest.fixture
def documents():
    return _documents()


def _documents(parser=None, suffix=''):
    state = LearningState(InMemoryMaterialRepository(), source_retriever=KeywordRetriever())
    if parser is not None:
        state.material_service.parser = parser
    uploads = [state.material_service.create(filename=f'{name}.md',
        content=f'# {name}\n\n{name} facts.\n\n# Detail\n\nAdditional {name} detail.{suffix}'.encode(),
        idempotency_key='reading-'+name) for name in ('Alpha', 'Beta', 'Foreign')]
    space = state.create_space({'material_ids': [u.material.id for u in uploads[:2]]})
    generator = FixedAnswer()
    state.message_service.generator = generator
    return state, space, uploads, generator


def test_contract_accepts_reading_and_rejects_mixed_lesson():
    body = {'material_id': 'm', 'material_version_id': 'v'}
    assert SendMessage(message='Explain', reading_context=body).model_dump()['reading_context'] == body
    with pytest.raises(ValidationError):
        SendMessage(message='Explain', reading_context=body, lesson_id='lesson')


@pytest.mark.parametrize('body', [{}, {'material_id': 'm'}, {'material_id': 'm', 'material_version_id': ''},
    {'material_id': 'm', 'material_version_id': 'v', 'extra': True}])
def test_contract_rejects_invalid_reading_identity(body):
    with pytest.raises(ValidationError):
        SendMessage(message='Explain', reading_context=body)


def test_reading_selects_only_current_document_and_freezes_history_identity(documents):
    state, space, _, generator = documents
    ctx = reading(space)
    sent = state.send_message(space['id'], {'message': 'Explain Alpha', 'reading_context': ctx})
    ctx['material_id'] = 'mutated'
    snap = generator.calls[-1]
    assert snap['reading_context'] == reading(space)
    assert snap['request_scope']['material_ids'] == [reading(space)['material_id']]
    assert {s['material_id'] for s in snap['sources']} == {reading(space)['material_id']}
    item = state.message_service.history(space['id'], sent['session_id'])['items'][0]
    assert item['reading_context'] == reading(space)
    assert item['lesson_id'] is None


@pytest.mark.parametrize('kind,code', [('foreign', 'MATERIAL_OUT_OF_SCOPE'), ('version', 'STALE_LEARNING_CONTEXT'),
    ('selection', 'READING_CONTEXT_MISMATCH')])
def test_reading_rejects_invalid_binding_before_dispatch(documents, kind, code):
    state, space, uploads, generator = documents
    ctx = reading(space)
    body = {'message': 'Explain', 'reading_context': ctx}
    if kind == 'foreign': ctx.update(material_id=uploads[2].material.id, material_version_id=uploads[2].version.id)
    if kind == 'version': ctx['material_version_id'] = 'old-version'
    if kind == 'selection': body['material_ids'] = [reading(space, 1)['material_id']]
    with pytest.raises(DomainConflict) as exc:
        state.send_message(space['id'], body, dispatch=lambda *a: pytest.fail('dispatched invalid context'))
    assert exc.value.code == code and generator.calls == []


def test_reading_conversation_cannot_be_reused_for_another_context(documents):
    state, space, _, generator = documents
    sent = state.send_message(space['id'], {'message': 'Explain', 'reading_context': reading(space)})
    for extra in ({}, {'reading_context': reading(space, 1)}, {'lesson_id': 'different-lesson'}):
        with pytest.raises(DomainConflict) as exc:
            state.send_message(space['id'], {'message': 'Explain again', 'session_id': sent['session_id'], **extra})
        assert exc.value.code == 'READING_CONTEXT_MISMATCH'
    assert len(generator.calls) == 1


def test_reading_memory_does_not_recall_global_or_other_document_turns(documents):
    state, space, _, generator = documents
    state.send_message(space['id'], {'message': 'Alpha global secret'})
    state.send_message(space['id'], {'message': 'Alpha foreign secret', 'reading_context': reading(space, 1)})
    first = state.send_message(space['id'], {'message': 'Alpha own explanation', 'reading_context': reading(space)})
    snap = generator.calls[-1]
    assert snap['history'] == [] and snap['memory'] == {'summary': {}, 'recall': []}
    state.send_message(space['id'], {'message': 'Explain Alpha again', 'reading_context': reading(space), 'session_id': first['session_id']})
    assert generator.calls[-1]['history'][0]['content'] == 'Alpha own explanation'
    state.send_message(space['id'], {'message': 'Alpha global query'})
    assert all(r['user'] != 'Alpha own explanation' for r in generator.calls[-1]['memory']['recall'])


def test_reading_refuses_lesson_session_without_inheriting_lesson():
    state, sid, node, payload = setup_lesson()
    lesson = state.lesson_service.create(sid, node, payload)
    state.lesson_service.generator = Generator()
    ModelTaskWorker(lessons=state.lesson_service).run_once()
    state.message_service.generator = FixedAnswer()
    state.message_service.retriever = KeywordRetriever()
    sent = state.send_message(sid, {'message': 'Explain lesson', 'lesson_id': lesson['lesson_id']})
    with pytest.raises(DomainConflict) as exc:
        state.send_message(sid, {'message': 'Explain original', 'reading_context': reading(state.get_space(sid)), 'session_id': sent['session_id']})
    assert exc.value.code == 'READING_CONTEXT_MISMATCH'
    state.send_message(sid, {'message': 'Explain original', 'reading_context': reading(state.get_space(sid))})
    snap = state.message_service.generator.calls[-1]
    assert 'lesson_id' not in snap and snap['history'] == [] and not snap['memory']['summary']


@pytest.mark.parametrize('backend', ['capturing', 'vector'])
def test_reading_source_candidates_cover_document_beyond_lesson_node(monkeypatch, backend):
    class UnmappedSourceParser(MaterialParser):
        def parse(self, content, *, filename):
            chunks = super().parse(content, filename=filename)
            extra = next(chunk for chunk in chunks if chunk.text == 'Unmapped original detail.')
            for unit in chunks.manifest['units']:
                unit['chunk_ids'] = [identifier for identifier in unit['chunk_ids'] if identifier != extra.id]
            return chunks
    state, space, _, generator = _documents(UnmappedSourceParser(), '\n\nUnmapped original detail.')
    ctx = reading(space)
    # The immutable parse product includes a chunk not selected by graph extraction.
    version = state.material_service.repository.get_version(ctx['material_version_id'])
    extra = next(chunk for chunk in version.chunks if chunk.text == 'Unmapped original detail.')
    bound_topics = state.space_service.bound_topics
    def mapped_topics(current):
        topics = bound_topics(current)
        for topic in topics:
            topic['source_refs'] = [r for r in topic['source_refs'] if r['chunk_id'] != extra.id]
        return topics
    monkeypatch.setattr(state.space_service, 'bound_topics', mapped_topics)
    candidates = []
    class Retriever:
        def select(self, message, sources, **kwargs):
            candidates.extend(copy.deepcopy(sources))
            return [next(s for s in sources if s['chunk_id'] == extra.id)]
    if backend == 'capturing':
        state.message_service.retriever = Retriever()
    else:
        from knowpath_backend.learning.rag.retrieval import VectorRetriever, point_id
        class Embedder:
            def embed(self, texts, **kwargs):
                return [[1.0]]
        class VectorBackend:
            def require_index(self, sources):
                # The real vector adapter accepts chunk identities without a
                # invented topic_id and checks every permitted candidate.
                assert len({point_id(s) for s in sources}) == len(version.chunks)
                candidates.extend(copy.deepcopy(sources))
            def search(self, vector, sources, **kwargs):
                return [next(s for s in sources if s['chunk_id'] == extra.id)]
        state.message_service.retriever = VectorRetriever(Embedder(), VectorBackend())
    sent = state.send_message(space['id'], {'message': 'Explain original detail', 'reading_context': ctx})
    assert state.get_run(sent['run_id'])['status'] == 'succeeded'
    assert {s['chunk_id'] for s in candidates} == {c.id for c in version.chunks}
    assert 'topic_id' not in generator.calls[-1]['sources'][0]


def test_reading_respects_selected_and_excluded_topics(documents):
    state, space, _, generator = documents
    ctx = reading(space)
    topics = [t for t in state.space_service.bound_topics(space) if any(r['material_id'] == ctx['material_id'] for r in t['source_refs'])]
    denied = {r['chunk_id'] for r in topics[0]['source_refs']}
    selected = [t['id'] for t in topics if not any(r['chunk_id'] in denied for r in t['source_refs'])]
    state.set_scope(space['id'], {'topic_ids': selected, 'excluded_topic_ids': [topics[0]['id']], 'expected_version': space['space_version']})
    state.send_message(space['id'], {'message': 'Explain detail', 'reading_context': ctx})
    assert all(s['chunk_id'] not in denied for s in generator.calls[-1]['sources'])


def test_reading_scope_persists_across_worker_restart(workspace):
    factory, sid, _, _ = workspace
    state, initial_generator = service(factory)
    ctx = reading(state.get_space(sid))
    jobs = []
    sent = state.send_message(sid, {'message': 'Explain functions', 'reading_context': ctx}, durable=True,
        dispatch=lambda fn, identifier: jobs.append(identifier))
    restored, generator = service(factory)
    ModelTaskWorker(messages=restored.message_service).run_once(jobs[0])
    assert not initial_generator.calls and generator.calls[0]['reading_context'] == ctx
    history = factory().message_service.history(sid, sent['session_id'])
    assert history['items'][0]['reading_context'] == ctx


def test_reading_binding_change_during_provider_call_withdraws_stream(documents):
    state, space, _, _ = documents
    class Change:
        def stream(self, snapshot, on_text):
            topic = state.space_service.bound_topics(space)[0]['id']
            state.set_scope(space['id'], {'topic_ids': [topic], 'expected_version': state.get_space(space['id'])['space_version']})
            on_text('Must not survive')
            return {'text': 'Must not survive', 'citation_ids': [snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator = Change()
    sent = state.send_message(space['id'], {'message': 'Explain', 'reading_context': reading(space)})
    run = state.get_run(sent['run_id'])
    assert run['status'] == 'failed' and run['error']['code'] == 'STALE_LEARNING_CONTEXT'
    assert not any(e['event'] == 'message.completed' for e in run['events'])


@pytest.mark.parametrize('durable', [False, True])
def test_reading_cancel_during_provider_stream_prevents_late_text(documents, durable):
    state, space, _, _ = documents
    class Cancel:
        def stream(self, snapshot, on_text):
            on_text('first draft')
            run_id = state.message_service.repository.records('messages', space_id=space['id'])[0]['run_id']
            state.cancel_run(run_id)
            on_text('late draft' * 20)
            return {'text': 'late answer', 'citation_ids': [snapshot['sources'][0]['chunk_id']]}
    state.message_service.generator = Cancel()
    sent = state.send_message(space['id'], {'message': 'Explain original', 'reading_context': reading(space)}, durable=durable)
    if durable:
        ModelTaskWorker(messages=state.message_service, max_attempts=1).run_once()
    run = state.run_service.get(sent['run_id'])
    assert run['status'] == 'cancelled'
    assert not any(e['event'] == 'message.delta' and e['data'].get('delta') for e in run['events'])
    assert not any(e['event'] == 'message.completed' for e in run['events'])
    item = state.message_service.history(space['id'], sent['session_id'])['items'][0]
    assert item['status'] == 'cancelled' and item['response'] is None


def test_reading_uses_source_bound_chain_when_main_pipeline_is_configured(documents):
    state, space, _, generator = documents
    class Pipeline:
        def answer(self, *a, **kw):
            pytest.fail('reading must not enter the all-space publication pipeline')
    state.message_service.rag_pipeline = Pipeline()
    sent = state.send_message(space['id'], {'message': 'Explain Alpha', 'reading_context': reading(space)})
    assert generator.calls and state.get_run(sent['run_id'])['status'] == 'succeeded'
    snap = state.message_service.repository.records('messages', run_id=sent['run_id'])[0]['snapshot']
    assert 'rag_execution_started' not in snap


def test_reading_configured_vector_retriever_fails_closed_for_unindexed_original(documents):
    from knowpath_backend.learning.rag.retrieval import VectorRetriever, RetrievalError, point_id
    state, space, _, generator = documents
    expected = state.message_service._reading_sources(space, reading(space))
    class Backend:
        def require_index(self, sources):
            assert {point_id(s) for s in sources} == {point_id(s) for s in expected}
            raise RetrievalError('VECTOR_INDEX_NOT_READY')
    state.message_service.retriever = VectorRetriever(None, Backend())
    sent = state.send_message(space['id'], {'message': 'Explain Alpha', 'reading_context': reading(space)})
    assert not generator.calls
    assert state.get_run(sent['run_id'])['error']['code'] == 'VECTOR_INDEX_NOT_READY'


def test_reading_completed_help_history_and_run_replay_are_audited(workspace):
    from knowpath_backend.test.test_learning_state_persistence import create
    factory, sid, _, _ = workspace
    state, generator = service(factory)
    assessed = create(state, sid)
    sent = state.send_message(sid, {'message': 'Explain functions', 'reading_context': reading(state.get_space(sid))})
    repo = state.message_service.repository
    stored = repo.get_record('assessments', assessed['id'])
    assert all(q['assisted'] for q in stored['questions'])
    assert any(e['kind'] == 'message' for e in stored['snapshot']['assistance_events'])
    state.message_service.history(sid, sent['session_id'])
    state.message_service.read_run(sent['run_id'])
    kinds = {e['kind'] for e in repo.get_record('assessments', assessed['id'])['snapshot']['assistance_events']}
    assert {'message', 'conversation_read', 'message_replay'} <= kinds


def test_null_reading_preserves_legacy_idempotency(workspace):
    factory, sid, _, _ = workspace
    state, generator = service(factory)
    first = state.send_message(sid, {'message': 'Explain functions'}, idempotency_key='reading-null')
    assert state.send_message(sid, {'message': 'Explain functions', 'reading_context': None}, idempotency_key='reading-null') == first
    assert len(generator.calls) == 1


def test_http_reading_contract_and_history_migration_fields():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.config import LearningSettings
    app = create_app(settings=LearningSettings(), answer_generator=FixedAnswer(), source_retriever=KeywordRetriever())
    state = app.state.learning_state
    upload = state.material_service.create(filename='reading.md', content=b'# Alpha\n\nOriginal document facts.', idempotency_key='http-reading')
    space = state.create_space({'material_ids': [upload.material.id]})
    with TestClient(app) as client:
        url = f"/api/v1/learning-spaces/{space['id']}/messages"
        headers = {'Idempotency-Key': 'http-reading-message'}
        assert client.post(url, json={'message': 'Explain', 'reading_context': reading(space), 'lesson_id': 'lesson'}, headers=headers).status_code == 422
        old = {**reading(space), 'material_version_id': 'old'}
        rejected = client.post(url, json={'message': 'Explain', 'reading_context': old}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'STALE_LEARNING_CONTEXT'
        accepted = client.post(url, json={'message': 'Explain', 'reading_context': reading(space)}, headers=headers)
        assert accepted.status_code == 202
        history_url = f"/api/v1/learning-spaces/{space['id']}/conversations/{accepted.json()['session_id']}/messages"
        item = client.get(history_url).json()['items'][0]
        assert item['reading_context'] == reading(space) and item['lesson_id'] is None
        events = client.get('/api/v1/runs/' + accepted.json()['run_id'] + '/events')
        assert events.status_code == 200 and 'event: message.completed' in events.text
        assert reading(space)['material_version_id'] in events.text
