import importlib
import json

import pytest

from knowpath_backend.core.model_port import SDKModelPort
from knowpath_backend.learning.agent_memory.service import MemorySessionService
from knowpath_backend.learning.rag.verification import VerificationError
from knowpath_backend.test.test_reading_messages import _documents, reading
from knowpath_backend.test.test_web_agent_runtime import output, claim
from knowpath_backend.test.test_rag_verification_v2 import verdict
from knowpath_backend.test.web_agent_support import ScriptedChatClient, completion


def api():
    return importlib.import_module('knowpath_backend.learning.agent_runtime.web')


def pending(*, mode='auto', read=False):
    state, space, uploads, _ = _documents()
    payload = {'message': 'Hello' if mode == 'auto' else 'Explain Alpha facts', 'answer_mode': mode}
    if read:
        payload['reading_context'] = reading(space)
    state.message_service.send(space['id'], payload, dispatch=lambda *args: None)
    service = state.message_service
    message = service.repository.records('messages', space_id=space['id'])[0]
    repo = service.repository.agent_memory_factory(space['id'])
    receipt = message['snapshot']['memory_scope']
    policy = service._build_memory_policy(repo, receipt, service.spaces.materials)
    session = MemorySessionService(repo).load(message['conversation_id'], receipt['context_epoch'],
        receipt['fingerprint'], eligible=lambda value, purpose: policy.eligible(value, purpose=purpose))
    return service, message, session, policy


def test_default_keyword_greeting_does_not_need_versioned_index():
    service, message, session, policy = pending()
    client = ScriptedChatClient([completion(output())])
    try:
        candidate = api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert candidate.text == 'Hello' and candidate.citation_ids == []
        assert len(client.requests) == 1
        assert service.repository.get_record('messages', message['id'])['status'] == 'pending'
        assert candidate.route['semantic_route']['scope_uncertain'] is True
    finally:
        session.close()


def test_legacy_material_uses_real_retriever_and_same_port_checker():
    service, message, session, policy = pending(mode='material', read=True)
    source = message['snapshot']['sources'][0]
    raw = output(route='material', text='UNVERIFIED', claims=[{**claim(), 'text': source['text']}])
    client = ScriptedChatClient([completion(raw), completion(json.dumps(verdict()))])
    try:
        candidate = api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert candidate.material_text == source['text']
        assert candidate.citation_ids == [source['chunk_id']]
        assert candidate.usage['model_calls'] == 2 and len(client.requests) == 2
        assert candidate.delivered_sources[0]['citation_schema_version'] == 2
        assert candidate.delivered_sources[0]['source_spans'][0]['artifact_hash']
        assert candidate.usage['call_journal']['physical_calls'] == 2
    finally:
        session.close()


def test_retriever_cannot_return_foreign_or_mutated_source():
    service, message, session, policy = pending(mode='material')
    service.retriever.select = lambda query, sources, limit: [{**sources[0], 'text': 'FORGED'}]
    client = ScriptedChatClient([])
    try:
        with pytest.raises(VerificationError, match='RAG_SOURCE_INVALID'):
            api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert client.requests == []
    finally:
        session.close()


def test_legacy_preparation_hash_checks_authoritative_chunk():
    service, message, session, policy = pending(mode='material')
    message['snapshot']['sources'][0]['text'] = 'FORGED'
    message['snapshot']['retrieval_sources'] = message['snapshot']['sources']
    client = ScriptedChatClient([])
    try:
        with pytest.raises(VerificationError, match='RAG_SOURCE_INVALID'):
            api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert client.requests == []
    finally:
        session.close()


def test_reading_retrieval_never_receives_other_document():
    service, message, session, policy = pending(mode='material', read=True)
    source = message['snapshot']['sources'][0]
    seen = []
    original = service.retriever.select
    def select(query, sources, limit):
        seen.extend(sources)
        return original(query, sources, limit=limit)
    service.retriever.select = select
    client = ScriptedChatClient([completion(output(route='material', claims=[{**claim(), 'text': source['text']}])),
                                 completion(json.dumps(verdict()))])
    try:
        api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert seen and all(row['material_id'] == message['snapshot']['reading_context']['material_id'] for row in seen)
    finally:
        session.close()


def test_invalid_checker_fails_without_independent_generation():
    service, message, session, policy = pending(mode='material')
    client = ScriptedChatClient([completion(output(route='material', claims=[claim()])), completion('{}')])
    try:
        with pytest.raises(VerificationError):
            api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert len(client.requests) == 2
        assert client.requests[1]['response_format']['type'] in ('json_object', 'json_schema')
    finally:
        session.close()


def test_configured_preparation_shares_budget_and_authoritative_allowlist():
    from types import SimpleNamespace
    from knowpath_backend.learning.rag.runtime import RequestTurnBudget
    from knowpath_backend.learning.rag.evidence import PreparedEvidence
    import time
    service, message, session, policy = pending(mode='material', read=True)
    prepared_args = []
    stored = {}
    class Pipeline:
        repo = SimpleNamespace(get_chunk=lambda version, chunk: stored.get(chunk))
        def new_turn_budget(self, *, deadline):
            self.budget = RequestTurnBudget(deadline)
            return self.budget
        def prepare_evidence(self, query, **kwargs):
            prepared_args.append(kwargs)
            row = dict(kwargs['allowed_sources'][0], retrieval_version_id='retrieval-1')
            stored[row['chunk_id']] = row
            return PreparedEvidence('ready', query, [row], {}, None, self.budget.deadline, time.monotonic())
    pipeline = Pipeline()
    service.rag_pipeline = pipeline
    row = message['snapshot']['sources'][0]
    # A fake retrieval store exercises adapter ownership. Production policy
    # retrieval-version authority remains covered by repository integration.
    policy.dependency_resolver = lambda dep: True
    client = ScriptedChatClient([completion(output(route='material', claims=[{**claim(), 'text': row['text']}])),
                                 completion(json.dumps(verdict()))])
    try:
        result = api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert prepared_args[0]['turn_budget'] is pipeline.budget
        assert prepared_args[0]['allowed_sources'][0]['source_spans']
        assert result.usage['model_calls'] == 2
    finally:
        session.close()


def test_adapter_preserves_explicit_greeting_semantic_audit():
    service, message, session, policy = pending()
    raw = json.loads(output())
    raw['semantic_route'] = dict(document_related=False, educational=False, topic_ids=[], scope_uncertain=False, reason='Greeting')
    client = ScriptedChatClient([completion(json.dumps(raw))])
    try:
        candidate = api().run_web_turn(service, message, session, policy, model_port=SDKModelPort(client))
        assert candidate.route['semantic_route']['educational'] is False
        assert candidate.route['assistance_scope'] is None
        assert candidate.route['status'] == 'answered'
    finally:
        session.close()
