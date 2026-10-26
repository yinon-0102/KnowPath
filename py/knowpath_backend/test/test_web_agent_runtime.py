import importlib
import json
import time
from dataclasses import replace

import pytest

from knowpath_backend.core.model_port import SDKModelPort
from knowpath_backend.core.turn import TurnCancelled, TurnBudgetExceeded
from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.learning.agent_runtime.contracts import TurnRequest, TurnBudgetLimits
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.evidence import PreparedEvidence
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.test.test_web_agent_memory_scope import frozen, policy
from knowpath_backend.test.web_agent_support import ScriptedChatClient, completion


def api():
    return importlib.import_module('knowpath_backend.learning.agent_runtime.runtime')


def output(*, route='general', text='Hello', claims=None):
    return json.dumps(dict(route=route, text=text, status='answered', claims=claims or [],
        required_points=[{'point_id': 'p1', 'text': 'Respond to question'}], missing_points=[]))


def claim(citation='E1'):
    return dict(claim_id='c1', text='Verified fact', kind='fact', citation_ids=[citation], depends_on=[])


@pytest.fixture
def memory():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'), context_policy=WebContextPolicy())
    yield manager
    manager.close()


def request(mode='auto', question='Hello'):
    return TurnRequest('m', 'r', 's', 'c', question, answer_mode=mode, context_epoch=1,
        scope_fingerprint=frozen()['fingerprint'], context_fingerprint=frozen()['fingerprint'], space_sequence=1)


def run(memory, responses, *, req=None, prepare=None, verify=None, events=None, should_cancel=None, allowed_topics=()):
    client = ScriptedChatClient(responses)
    runtime = api().WebAgentRuntime(LearningSettings(), model_port=SDKModelPort(client))
    result = runtime.run(req or request(), memory=memory, policy=policy(),
        prepare_evidence=prepare or (lambda query: pytest.fail('Unexpected search')),
        verify_candidate=verify or (lambda *args, **kw: pytest.fail('Unexpected checker')),
        on_event=events.append if events is not None else None, should_cancel=should_cancel, allowed_topic_ids=allowed_topics)
    return result, client


def prepared():
    return PreparedEvidence('ready', 'question', [dict(material_id='a', material_version_id='a-v1',
        chunk_id='chunk-a', source_text='Verified fact')], {}, None, time.monotonic() + 60, time.monotonic())


def test_greeting_uses_real_agent_and_does_not_publish(memory):
    events = []
    result, client = run(memory, [completion(output())], events=events)
    assert result.text == 'Hello'
    assert len(client.requests) == 1 and memory.working.items() == []
    assert result.source_dependencies == [{'message_id': 'm'}]
    assert all(set(event) <= {'type', 'status'} for event in events)


def test_material_forces_search_and_checks_draft_with_shared_port(memory):
    calls = []
    def verify(question, sources, candidate, *, deadline, model_port):
        calls.append((sources, candidate, model_port))
        assert candidate.claims[0].citation_ids == ('chunk-a',)
        return dict(status='answered', text='Verified fact', citation_ids=['chunk-a'],
                    claims=[candidate.claims[0].model_dump()], trace={})
    result, client = run(memory, [completion(output(route='material', text='UNVERIFIED prose', claims=[claim()]))],
        req=request('material'), prepare=lambda q: prepared(), verify=verify)
    assert result.text == 'Verified fact' and 'UNVERIFIED' not in result.text
    assert result.citation_ids == ['chunk-a'] and result.delivered_sources[0]['chunk_id'] == 'chunk-a'
    assert len(calls) == 1 and calls[0][2].calls == 1
    assert 'E1' in client.requests[0]['messages'][-1]['content']


def test_history_query_requires_actual_recall(memory):
    result, client = run(memory, [completion(output(text='I have no previous record'))], req=request('auto', 'What did I say previously?'))
    assert any(row['name'] == 'recall' for row in result.tool_trace)


def test_forged_citation_never_reaches_checker(memory):
    with pytest.raises(ValueError, match='citation'):
        run(memory, [completion(output(route='material', claims=[claim('E999')]))],
            req=request('material'), prepare=lambda q: prepared())


def test_general_cannot_smuggle_material_claims(memory):
    with pytest.raises(ValueError):
        run(memory, [completion(output(claims=[claim()]))])


def test_cancel_prevents_model_and_evidence(memory):
    with pytest.raises(TurnCancelled):
        run(memory, [], req=request('material'), should_cancel=lambda: True)


def test_repair_cannot_reset_exhausted_budget(memory):
    def verify(*args, **kwargs):
        return dict(status='insufficient', text='Rejected', citation_ids=[], claims=[], trace={})
    req = replace(request('material'), budget=TurnBudgetLimits(max_model_calls=1))
    with pytest.raises(TurnBudgetExceeded):
        run(memory, [completion(output(route='material', claims=[claim()]))], req=req, prepare=lambda q: prepared(), verify=verify)


def test_auto_search_tool_delivers_registered_sources(memory):
    def verify(question, sources, draft, **kwargs):
        return dict(status='answered', text='Verified fact', citation_ids=['chunk-a'], claims=[draft.claims[0].model_dump()])
    result, client = run(memory, [completion(tool_calls=[('s', 'search', {'query': 'question'})]),
        completion(output(route='material', claims=[claim()]))], prepare=lambda q: prepared(), verify=verify)
    assert result.tool_trace == [{'name': 'search'}]
    assert result.delivered_sources[0]['chunk_id'] == 'chunk-a'
    assert len(client.requests) == 2


def test_one_repair_and_checker_calls_share_identical_budget(memory):
    seen = []
    def verify(question, sources, draft, *, deadline, model_port):
        seen.append(model_port)
        model_port.complete(dict(model='offline', messages=[{'role': 'user', 'content': 'check'}], max_tokens=20), stream=False)
        return dict(status='insufficient' if len(seen) == 1 else 'answered', text='Verified fact',
            citation_ids=[] if len(seen) == 1 else ['chunk-a'], claims=[] if len(seen) == 1 else [draft.claims[0].model_dump()])
    result, client = run(memory, [completion(output(route='material', claims=[claim()])), completion('checker 1'),
        completion(output(route='material', claims=[claim()])), completion('checker 2')],
        req=request('material'), prepare=lambda q: prepared(), verify=verify)
    assert seen[0] is seen[1] and result.usage['model_calls'] == 4
    assert len(client.requests) == 4


def test_general_cannot_escape_evidence_check_after_search(memory):
    with pytest.raises(ValueError, match='general'):
        run(memory, [completion(tool_calls=[('s', 'search', {'query': 'question'})]), completion(output())],
            prepare=lambda q: prepared())


def test_real_l0_card_consumption_uses_card_reference(memory):
    from knowpath_backend.memory.playbook import L0Source
    card = memory.remember('User prefers concise explanations', source=L0Source.LLM_REMEMBERED,
        metadata={'memory_scope': frozen(), 'source_dependencies': [{'message_id': 'past'}]})
    result, client = run(memory, [completion(output())])
    assert {'kind': 'card', 'id': card.id} in result.source_dependencies
    assert {'item_id': card.id} not in result.source_dependencies


def test_ineligible_l0_card_not_scored_refreshed_or_supplied(memory, monkeypatch):
    from knowpath_backend.memory.playbook import L0Source
    card = memory.remember('PRIVATE excluded fact', source=L0Source.LLM_REMEMBERED)
    monkeypatch.setattr(type(card), 'refresh', lambda *args, **kwargs: pytest.fail('ineligible card refreshed'))
    result, client = run(memory, [completion(output())])
    assert 'PRIVATE' not in json.dumps(client.requests)


def test_material_partial_status_is_preserved(memory):
    def verify(question, sources, draft, **kwargs):
        return dict(status='partial', text='Verified portion', citation_ids=['chunk-a'], claims=[draft.claims[0].model_dump()])
    result, _ = run(memory, [completion(output(route='material', claims=[claim()]))],
        req=request('material'), prepare=lambda query: prepared(), verify=verify)
    assert result.route['status'] == 'partial'


def test_explicit_greeting_semantics_do_not_mark_assistance(memory):
    raw = json.loads(output())
    raw['semantic_route'] = dict(document_related=False, educational=False, topic_ids=[], scope_uncertain=False, reason='Greeting')
    result, client = run(memory, [completion(json.dumps(raw))])
    assert result.route['semantic_route'] == raw['semantic_route']
    assert result.route['assistance_scope'] is None
    assert len(client.requests) == 1


def test_unspecified_semantic_route_defaults_to_conservative_audit(memory):
    result, _ = run(memory, [completion(output())])
    assert result.route['semantic_route']['educational'] is True
    assert result.route['assistance_scope'] == {'topic_ids': [], 'all_topics': True}


def test_model_cannot_invent_semantic_topic_ids(memory):
    raw = json.loads(output())
    raw['semantic_route'] = dict(document_related=False, educational=True, topic_ids=['foreign'], scope_uncertain=False, reason='Help')
    with pytest.raises(ValueError, match='topic'):
        run(memory, [completion(json.dumps(raw))])


def test_general_status_is_preserved(memory):
    raw = json.loads(output())
    raw['status'] = 'clarify'
    result, _ = run(memory, [completion(json.dumps(raw))])
    assert result.route['status'] == 'clarify'


def test_authorized_semantic_topic_help_is_audited_without_extra_call(memory):
    raw = json.loads(output())
    raw['semantic_route'] = dict(document_related=False, educational=True, topic_ids=['topic-a'], scope_uncertain=False, reason='Learning explanation')
    result, client = run(memory, [completion(json.dumps(raw))], allowed_topics=['topic-a'])
    assert result.route['assistance_scope'] == {'topic_ids': ['topic-a'], 'all_topics': False}
    assert len(client.requests) == 1


@pytest.mark.parametrize('route', [
    dict(document_related=True, educational=False, topic_ids=[], scope_uncertain=False, reason='Wrong route'),
    dict(document_related=False, educational=False, topic_ids=[], scope_uncertain=True, reason='Inconsistent greeting'),
])
def test_inconsistent_semantic_route_fails_closed(memory, route):
    raw = json.loads(output())
    raw['semantic_route'] = route
    with pytest.raises(ValueError):
        run(memory, [completion(json.dumps(raw))])
