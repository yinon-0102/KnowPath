from dataclasses import replace
import json
import time

import httpx
import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError


def test_strict_schema_sent_and_part_of_counted_request(monkeypatch):
    monkeypatch.setenv('CAPACITY_KEY', 'SECRET')
    monkeypatch.setenv('RAG_RESPONSE_FORMAT', 'json_schema')
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop',
            'message':{'role':'assistant','content':'{"ok":true}'}}], 'usage':{'prompt_tokens':10}})
    schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = BudgetedJsonModel(LearningSettings(chat_api_key_env='CAPACITY_KEY'), client=client)
        model.generate_json([{'role':'user','content':'JSON'}], deadline=time.monotonic()+5, response_schema=schema)
    assert requests[0]['response_format']['json_schema']['schema'] == schema
    assert requests[0]['response_format']['json_schema']['strict'] is True
    assert model.last_usage['count_kind'] == 'conservative_upper_bound'
    assert model.last_usage['actual_prompt_tokens'] == 10


def test_context_packing_reserves_checker_draft_before_first_model_call():
    from knowpath_backend.learning.rag.capacity import AnswerCapacity
    settings = LearningSettings(context_budget_tokens=32000)
    generator = BudgetedJsonModel(settings, max_input_tokens=14000)
    checker = BudgetedJsonModel(settings, max_input_tokens=14000)
    verifier = AnswerVerifier(generator, checker)
    capacity = AnswerCapacity(verifier, generation_seconds=1, verification_seconds=1)
    rows = [dict(chunk_id='x', source_text='规则。'*1200, evidence_group='x', requires=[]),
            dict(chunk_id='y', source_text='短规则。', evidence_group='y', requires=[])]
    context, trace = capacity.select_context('问题', rows)
    assert [r['chunk_id'] for r in context] == ['y']
    assert trace['omitted_chunk_ids'] == ['x']
    assert trace['draft_reserve_bytes'] == 4000


def test_revision_admission_reserves_a_new_draft_and_recheck_time():
    from knowpath_backend.learning.rag.capacity import AnswerCapacity
    generator = BudgetedJsonModel(LearningSettings(), max_input_tokens=12000)
    checker = BudgetedJsonModel(LearningSettings(), max_input_tokens=12000)
    capacity = AnswerCapacity(AnswerVerifier(generator, checker), generation_seconds=5, verification_seconds=7)
    messages = [{'role':'user','content':'{}'}]
    with pytest.raises(VerificationError, match='RAG_STAGE_BUDGET_EXCEEDED'):
        capacity.admit_revision(messages, messages, deadline=time.monotonic()+10)


def test_nonempty_evidence_that_exceeds_shared_context_is_not_missing_evidence():
    from knowpath_backend.learning.rag.capacity import AnswerCapacity
    settings = LearningSettings(context_budget_tokens=32000)
    model = BudgetedJsonModel(settings, max_input_tokens=24000)
    capacity = AnswerCapacity(AnswerVerifier(model, model), generation_seconds=1, verification_seconds=1)
    with pytest.raises(VerificationError, match='MODEL_TOKEN_BUDGET_EXCEEDED'):
        capacity.select_context('问题', [{'chunk_id':'c', 'source_text':'规'*2000}], max_evidence_tokens=5000)


def test_failure_journal_survives_eval_record_without_exposing_prompt(evaluation):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.test.test_rag_eval import frozen, resolver
    class Pipeline:
        def __init__(self, mode): pass
        def answer(self, *args, **kwargs):
            journal = RequestJournal()
            call = journal.begin_call('generation')
            journal.finish_call(call, status='succeeded', usage={'prompt_tokens':10})
            error = VerificationError('VERIFICATION_UNAVAILABLE')
            error.call_journal = journal.seal('failed')
            raise error
    records = run(frozen(evaluation), evaluation[0].parent/'failed.jsonl', pipeline_factory=Pipeline, scope_resolver=resolver)
    assert all(r['call_journal']['calls'][0]['usage']['prompt_tokens'] == 10 for r in records)


from knowpath_backend.test.test_rag_eval import evaluation


@pytest.mark.parametrize('key,value', [('RAG_MAX_DRAFT_BYTES','0'),
    ('RAG_REVISION_GENERATION_SECONDS','NaN'), ('RAG_REVISION_VERIFICATION_SECONDS','121'),
    ('RAG_RESPONSE_FORMAT','SECRET')])
def test_invalid_protocol_settings_fail_before_provider_construction(monkeypatch,key,value):
    from knowpath_backend.learning.rag.protocol_config import protocol_configuration
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError, match='^RAG_PROTOCOL_CONFIG_INVALID$'):
        protocol_configuration(LearningSettings())


def test_runtime_freeze_contains_exact_protocol_and_counter_provenance(monkeypatch):
    from knowpath_backend.rag_eval.cli import runtime_configuration
    monkeypatch.setenv('RAG_RESPONSE_FORMAT','json_schema')
    monkeypatch.setenv('RAG_MAX_DRAFT_BYTES','3500')
    config=runtime_configuration(LearningSettings())
    protocol=config['prompts']['protocol']
    assert protocol['response_format']=='json_schema'
    assert protocol['max_draft_bytes']==3500 and protocol['wire_version']==2
    assert protocol['evidence_locator']=='segment-id-v1'
    assert protocol['counting_profile']
    monkeypatch.setenv('RAG_MAX_DRAFT_BYTES','4000')
    assert runtime_configuration(LearningSettings()) != config
