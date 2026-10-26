"""Production message execution can publish a real generic Agent turn."""
import json
import pytest

from knowpath_backend.core.model_port import SDKModelPort
from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.web_agent_support import ScriptedChatClient, completion
from knowpath_backend.test.test_web_agent_runtime import output, claim
from knowpath_backend.test.test_rag_verification_v2 import verdict


def test_api_default_composition_selects_agent():
    from knowpath_backend.learning.api.application import create_app
    app = create_app()
    assert app.state.learning_state.message_service.use_agent_runtime is True


def test_production_http_message_and_sse_use_agent():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api.application import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename='api.md', content=b'# API\n\nAPI facts.', idempotency_key='api-agent')
    space = state.create_space({'material_ids': [material.material.id]})
    raw = json.loads(output(text='Hello via HTTP'))
    raw['semantic_route'] = {'document_related': False, 'educational': False, 'topic_ids': [],
                             'scope_uncertain': False, 'reason': 'Greeting'}
    client_model = ScriptedChatClient([completion(json.dumps(raw))])
    state.message_service.agent_model_port = SDKModelPort(client_model)
    with TestClient(app) as client:
        sent = client.post('/api/v1/learning-spaces/' + space['id'] + '/messages',
                           headers={'Idempotency-Key': 'http-agent-turn'},
                           json={'message': 'Hello', 'answer_mode': 'general'})
        assert sent.status_code == 202, sent.text
        run_id = sent.json()['run_id']
        events = client.get('/api/v1/runs/' + run_id + '/events')
        assert events.status_code == 200
        assert 'event: message.completed' in events.text and 'Hello via HTTP' in events.text
        assert len(client_model.requests) == 1
        message = state.message_service.repository.records('messages', space_id=space['id'])[0]
        assert message['snapshot']['semantic_route']['educational'] is False
        assert state.message_service.repository.agent_memory_factory(space['id']).get_commit('turn:' + message['id'])


@pytest.mark.parametrize('entry', ['chat', 'reading', 'lesson', 'assessment', 'worker'])
def test_real_agent_material_entries(entry, monkeypatch):
    from knowpath_backend.test.test_reading_messages import _documents, reading
    from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    if entry == 'lesson':
        state, sid, node, lesson_payload = setup_lesson()
        state.lesson_service.generator = Generator()
        created = state.lesson_service.create(sid, node, lesson_payload)
        assert ModelTaskWorker(lessons=state.lesson_service).run_once()
        extra = {'lesson_id': created['lesson_id']}
    else:
        state, space, _, _ = _documents()
        sid = space['id']
        extra = {'reading_context': reading(space)} if entry == 'reading' else {}
    service = state.message_service
    service.use_agent_runtime = True
    if entry == 'assessment':
        from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, create
        state.assessment_service.generator = FixedQuestions()
        create(state, sid)
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy generation or context projection executed')
    monkeypatch.setattr(service, '_adaptive_answer', forbidden)
    monkeypatch.setattr(service, '_legacy_answer', forbidden)
    monkeypatch.setattr('knowpath_backend.learning.conversations.service.project_memory', forbidden)
    result = service.send(sid, {'message': 'Explain this concept', 'answer_mode': 'material', **extra},
                          dispatch=lambda *args: None, durable=entry == 'worker')
    message = service.repository.records('messages', space_id=sid)[0]
    source = service.retriever.select(message['message'], message['snapshot']['sources'], limit=5)[0]
    client = ScriptedChatClient([
        completion(output(route='material', claims=[{**claim(), 'text': source['text']}])),
        completion(json.dumps(verdict())),
    ])
    service.agent_model_port = SDKModelPort(client)
    if entry == 'worker':
        assert ModelTaskWorker(messages=service).run_once()
    else:
        service.generate(message['id'])
    final = service.repository.get_record('messages', message['id'])
    assert final['status'] == 'completed', state.run_service.get(result['run_id'])
    assert final['response']['text'] == source['text']
    assert final['response']['citations'][0]['chunk_id'] == source['chunk_id']
    assert final['snapshot']['history'] == []
    assert len(client.requests) == 2
    assert service.repository.agent_memory_factory(sid).get_commit('turn:' + message['id'])
    events = state.events_for(result['run_id'])
    assert sum(event['event'] == 'message.completed' for event in events) == 1


def test_queued_agent_cannot_switch_to_old_generator(workspace):
    factory, sid, _, _ = workspace
    state = factory()
    service = state.message_service
    service.use_agent_runtime = True
    service.send(sid, {'message': 'Hello', 'answer_mode': 'general'}, dispatch=lambda *args: None)
    message = service.repository.records('messages', space_id=sid)[0]
    service.use_agent_runtime = False
    service.generate(message['id'])
    final = service.repository.get_record('messages', message['id'])
    assert final['status'] == 'failed'
    assert state.run_service.get(final['run_id'])['error']['code'] == 'AGENT_RUNTIME_VERSION_MISMATCH'


@pytest.mark.parametrize('code', ['RATE_LIMITED', 'UNSUPPORTED_MODEL'])
def test_agent_provider_error_preserves_public_code(workspace, code):
    from knowpath_backend.learning.providers.models import ModelError
    factory, sid, _, _ = workspace
    state = factory()
    service = state.message_service
    service.use_agent_runtime = True
    client = ScriptedChatClient([ModelError(code)])
    service.agent_model_port = SDKModelPort(client)
    sent = service.send(sid, {'message': 'Hello', 'answer_mode': 'general'})
    assert state.run_service.get(sent['run_id'])['error']['code'] == code
    assert len(client.requests) == 1


def test_web_message_uses_agent_and_commits_memory(workspace, monkeypatch):
    factory, sid, _, _ = workspace
    state = factory()
    service = state.message_service
    client = ScriptedChatClient([completion(json.dumps({
        'route': 'general', 'text': 'Hello from shared Agent', 'status': 'answered',
        'claims': [], 'required_points': [{'point_id': 'p1', 'text': 'Greet'}],
        'missing_points': [],
    }))])
    service.agent_model_port = SDKModelPort(client)
    service.use_agent_runtime = True
    def forbidden(*args, **kwargs):
        raise AssertionError('old answer pipeline executed')
    monkeypatch.setattr(service, '_adaptive_answer', forbidden)
    monkeypatch.setattr(service, '_legacy_answer', forbidden)
    result = state.send_message(sid, {'message': 'Hello', 'answer_mode': 'general'})
    message = service.repository.records('messages', space_id=sid)[0]
    assert message['status'] == 'completed'
    assert message['response']['text'] == 'Hello from shared Agent'
    assert len(client.requests) == 1
    memory = service.repository.agent_memory_factory(sid)
    assert memory.get_commit('turn:' + message['id']) is not None
    assert len(memory.items_for_turn(message['id'])) == 2
    assert [event['event'] for event in state.events_for(result['run_id'])].count('message.completed') == 1


def test_real_web_recall_and_remember_survive_restart(workspace):
    factory, sid, _, _ = workspace
    first = factory()
    first.message_service.use_agent_runtime = True
    marker = 'My favourite example is cobalt-lantern'
    first_client = ScriptedChatClient([
        completion(tool_calls=[('remember-one', 'remember', {'content': marker})]),
        completion(output(text='I will remember cobalt-lantern')),
    ])
    first.message_service.agent_model_port = SDKModelPort(first_client)
    sent = first.send_message(sid, {'message': marker, 'answer_mode': 'general'})
    old = first.message_service.repository.records('messages', space_id=sid)[0]
    assert old['status'] == 'completed'
    restored = factory()
    restored.message_service.use_agent_runtime = True
    second_client = ScriptedChatClient([completion(output(text='You mentioned cobalt-lantern'))])
    restored.message_service.agent_model_port = SDKModelPort(second_client)
    again = restored.send_message(sid, {'message': 'What did I say previously about cobalt-lantern?',
                                       'answer_mode': 'general'})
    final = next(row for row in restored.message_service.repository.records('messages', space_id=sid)
                 if row['run_id'] == again['run_id'])
    assert final['status'] == 'completed', restored.run_service.get(again['run_id'])
    data = json.loads(second_client.requests[0]['messages'][-1]['content'])
    recalled = next(row['result']['memories'] for row in data['initial_tool_data'] if row['tool'] == 'recall')
    assert any('cobalt-lantern' in row['text'] for row in recalled)
    repo = restored.message_service.repository.agent_memory_factory(sid)
    assert any(card.content == marker for card in repo.list_cards())
    assert any(dep.get('message_id') == old['id'] for dep in final['snapshot']['source_dependencies'])


def test_lesson_context_qualifies_original_spans_of_retrieval_chunks():
    from copy import deepcopy
    from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    from knowpath_backend.learning.rag.scope import legacy_span
    state, sid, node, lesson_payload = setup_lesson()
    state.lesson_service.generator = Generator()
    lesson = state.lesson_service.create(sid, node, lesson_payload)
    assert ModelTaskWorker(lessons=state.lesson_service).run_once()
    service = state.message_service
    service.use_agent_runtime = True
    service.send(sid, {'message': 'Explain', 'lesson_id': lesson['lesson_id'], 'answer_mode': 'material'},
                 dispatch=lambda *args: None)
    current = service.repository.records('messages', space_id=sid)[0]
    snapshot = deepcopy(current['snapshot'])
    source = snapshot['sources'][0]
    version = service.spaces.materials.get_version(source['material_version_id'])
    chunk = next(row for row in version.chunks if row.id == source['chunk_id'])
    snapshot['sources'] = [{**source, 'chunk_id': 'new-retrieval-id', 'citation_schema_version': 2,
                            'source_spans': [legacy_span(version.id, chunk).model_dump()]}]
    space = service.spaces.repository.get(sid)
    assert service._context_error(space, current, snapshot) is None
    snapshot['sources'][0]['source_spans'][0]['block'] = 'foreign-original'
    assert service._context_error(space, current, snapshot) == 'RETRIEVAL_SCOPE_INVALID'


def test_derived_summary_requalifies_original_items(workspace):
    from knowpath_backend.memory.summary import SummaryMemory
    factory, sid, _, _ = workspace
    state = factory()
    sent = state.send_message(sid, {'message': 'Explain functions'})
    service = state.message_service
    original = service.repository.records('messages', space_id=sid)[0]
    repo = service.repository.agent_memory_factory(sid)
    summary = SummaryMemory(track_provenance=True, flush_threshold=100)
    for item in repo.items_for_turn(original['id']):
        summary.add(item)
    derived = summary.flush()
    policy = service._build_memory_policy(repo, original['snapshot']['memory_scope'], service.spaces.materials)
    assert policy.eligible(derived, purpose='L2')
    with service.repository.transaction():
        source = service.repository.get_record('messages', original['id'])
        source['status'] = 'failed'
        service.repository.put_record('messages', source)
    assert not policy.eligible(derived, purpose='L2')
