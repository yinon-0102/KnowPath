"""Request-local identity constraints must match actual capacity envelopes."""
from copy import deepcopy
import json
import time

import httpx
import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.capacity import AnswerCapacity
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.protocol_config import protocol_configuration
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError, WireDraft, WireVerdict
from knowpath_backend.test.test_rag_verification_v2 import Model, SOURCE, draft, verdict, run


def models(monkeypatch):
    monkeypatch.setenv('RAG_RESPONSE_FORMAT','json_schema')
    settings = LearningSettings(context_budget_tokens=64000)
    return (BudgetedJsonModel(settings,max_input_tokens=60000),
            BudgetedJsonModel(settings,max_input_tokens=60000,stage='verification'))


def payload_messages(messages, payload):
    result = deepcopy(messages)
    result[-1]['content'] = json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    return result


def native_schema(body):
    return body['response_format']['json_schema']['schema']


def test_generator_sees_only_chunk_ids_and_exact_full_source_across_revision():
    generator, checker = Model([draft(),draft()]),Model([verdict('answer_incomplete'),verdict()])
    run(generator,checker)
    for messages in generator.calls:
        payload = json.loads(messages[-1]['content'])
        assert payload['evidence'] == [dict(chunk_id='s1',source_text=SOURCE['source_text'])]
        assert 'evidence_id' not in json.dumps(payload)
    for messages in checker.calls:
        payload = json.loads(messages[-1]['content'])
        assert ''.join(p['text'] for p in payload['evidence'][0]['segments']) == SOURCE['source_text']


def test_request_schema_enumerates_sources_without_mutating_base_or_other_requests(monkeypatch):
    generator, checker = models(monkeypatch)
    verifier = AnswerVerifier(generator,checker)
    initial, _ = verifier.initial_messages('问题', [SOURCE])
    base = WireDraft.model_json_schema()
    original = deepcopy(base)
    bound = native_schema(generator.request_body(initial,base))
    fields = bound['$defs']['WireClaim']['properties']
    assert fields['citation_ids']['items']['enum'] == ['s1']
    assert fields['claim_id']['enum'] == ['c'+str(i) for i in range(1,13)]
    assert fields['depends_on']['items']['enum'] == fields['claim_id']['enum']
    expanded, _ = verifier.initial_messages('问题', [SOURCE,dict(SOURCE,chunk_id='other')])
    assert native_schema(generator.request_body(expanded,base))['$defs']['WireClaim']['properties']['citation_ids']['items']['enum'] == ['s1','s2']
    assert base == original
    assert fields['citation_ids']['items']['enum'] == ['s1']


def test_checker_binds_exact_evidence_and_actual_claim_ids(monkeypatch):
    generator, checker = models(monkeypatch)
    _, planned = AnswerVerifier(generator,checker).initial_messages('问题',[SOURCE])
    payload = json.loads(planned[-1]['content'])
    payload['draft'] = draft()
    payload['draft']['claims'][0]['claim_id'] = 'c12'
    bound = native_schema(checker.request_body(payload_messages(planned,payload),WireVerdict.model_json_schema()))
    definitions = bound['$defs']
    assert definitions['WireCheck']['properties']['claim_id']['enum'] == ['c12']
    assert definitions['WireCheck']['properties']['citation_ids']['items']['enum'] == ['s1']
    assert definitions['WireRequirement']['properties']['claim_ids']['items']['enum'] == ['c12']
    assert definitions['WireEvidenceSpan']['properties']['evidence_id']['enum'] == ['e1']


def test_planned_checker_full_claim_pool_bounds_actual_request(monkeypatch):
    generator, checker = models(monkeypatch)
    verifier = AnswerVerifier(generator,checker)
    initial, planned = verifier.initial_messages('问题',[SOURCE])
    capacity = AnswerCapacity(verifier,generation_seconds=1,verification_seconds=1)
    requests = capacity._requests(initial,planned)
    plan = requests[1]
    pool = native_schema(plan.body)['$defs']['WireCheck']['properties']['claim_id']['enum']
    assert pool == ['c'+str(i) for i in range(1,13)]
    payload = json.loads(planned[-1]['content'])
    payload['draft'] = draft(text='引号"和反斜杠\\仍保留原始预算开销。')
    actual = checker.request_body(payload_messages(planned,payload),WireVerdict.model_json_schema())
    assert checker.counting_profile.count_request(actual).value <= (
        checker.counting_profile.count_request(plan.body).value + plan.reserved_input_tokens)
    assert capacity._requests(initial,payload_messages(planned,payload))[1].body == actual


def test_empty_claim_set_uses_valid_empty_arrays_not_empty_enum(monkeypatch):
    generator, checker = models(monkeypatch)
    _, messages = AnswerVerifier(generator,checker).initial_messages('问题',[SOURCE])
    payload = json.loads(messages[-1]['content'])
    payload['draft'] = draft('insufficient')
    schema = native_schema(checker.request_body(payload_messages(messages,payload),WireVerdict.model_json_schema()))
    assert schema['properties']['checks']['maxItems'] == 0
    assert schema['$defs']['WireRequirement']['properties']['claim_ids']['maxItems'] == 0
    def visit(value):
        if isinstance(value,dict):
            assert value.get('enum') != []
            for child in value.values(): visit(child)
        elif isinstance(value,list):
            for child in value: visit(child)
    visit(schema)


@pytest.mark.parametrize('claim_id',['c0','c13','claim1','e1'])
def test_wire_claim_ids_cannot_exceed_planned_pool_even_if_provider_ignores_schema(claim_id):
    raw = draft()
    raw['claims'][0]['claim_id'] = claim_id
    with pytest.raises(VerificationError,match='GENERATION_INVALID_RESPONSE'):
        run(Model([raw]),Model([]))


def test_math_hint_changes_only_generator_instruction_and_preserves_source():
    source = dict(SOURCE,source_text=r'实数符号 $\\mathbb{R}$。')
    initial, check = AnswerVerifier(Model([]),Model([])).initial_messages('定义？',[source])
    assert 'Unicode' in initial[0]['content']
    assert 'LaTeX' in initial[0]['content']
    assert '美元' in initial[0]['content']
    assert json.loads(initial[-1]['content'])['evidence'][0]['source_text'] == source['source_text']
    assert ''.join(p['text'] for p in json.loads(check[-1]['content'])['evidence'][0]['segments']) == source['source_text']


def test_new_identity_and_presentation_policy_is_frozen():
    config = protocol_configuration(LearningSettings())
    assert config['generator_evidence'] == 'whole-source-v1'
    assert config['identity_schema'] == 'request-enum-v1'
    assert config['math_notation'] == 'unicode-plain-v1'


def test_counted_native_schema_is_exactly_the_one_sent(monkeypatch):
    generator, checker = models(monkeypatch)
    monkeypatch.setenv(generator.settings.chat_api_key_env,'test-only')
    messages, _ = AnswerVerifier(generator,checker).initial_messages('问题',[SOURCE])
    expected = generator.request_body(messages,WireDraft.model_json_schema())
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':json.dumps(draft())}}], 'usage':{'prompt_tokens':30,'completion_tokens':30}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        generator.client = client
        generator.generate_json(messages,deadline=time.monotonic()+10,response_schema=WireDraft.model_json_schema())
    assert sent == [expected]
    assert generator.last_usage['estimated_input_tokens'] == generator.counting_profile.count_request(expected).value


def test_stop_with_truncated_json_is_still_failed_without_hidden_repair(monkeypatch):
    generator, _ = models(monkeypatch)
    monkeypatch.setenv(generator.settings.chat_api_key_env,'test-only')
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':'{"status":"answered","claims":[{"text":"实数符号 $'}}],
            'usage':{'prompt_tokens':30,'completion_tokens':48}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        generator.client = client
        with pytest.raises(VerificationError,match='MODEL_INVALID_RESPONSE'):
            generator.generate_json([{'role':'user','content':'JSON'}],deadline=time.monotonic()+10)
    assert len(sent) == 1
