"""Safe failure observability must not change budgets, retries, or answer outcomes."""
import json
import time
from dataclasses import replace

import httpx
import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.verification import AnswerVerifier, VerificationError
from knowpath_backend.test.test_rag_eval import evaluation, frozen, resolver
from knowpath_backend.test.test_rag_verification import Model, SOURCES, answer, verdict


def test_error_details_allow_only_typed_scalar_diagnostics():
    error = VerificationError('MODEL_INVALID_RESPONSE', details={
        'stage':'verification','call_index':2,'input_bound':123,'input_limit':12000,
        'output_limit':2000,'context_limit':16000,'failure_kind':'finish_reason',
        'finish_reason':'length','prompt_tokens':100,'completion_tokens':2000,
        'total_tokens':2100,'url':'private-url','prompt':'private-prompt','request_id':'private-id',
        'response':{'text':'private-response'},'messages':['private-message'],
        'input_tokens':True,'output_tokens':-1})
    assert error.details == {'stage':'verification','call_index':2,'input_bound':123,'input_limit':12000,
        'output_limit':2000,'context_limit':16000,'failure_kind':'finish_reason',
        'finish_reason':'length','prompt_tokens':100,'completion_tokens':2000,'total_tokens':2100}
    assert str(error)=='MODEL_INVALID_RESPONSE'
    assert 'private' not in repr(error.details)


@pytest.mark.parametrize('details',[{'stage':'private-secret','failure_kind':'private-secret',
    'finish_reason':'private-secret','call_index':True,'input_bound':float('inf')},
    {'stage':['generation'],'call_index':0,'prompt_tokens':'private-secret'}, ['private-secret']])
def test_unknown_or_invalid_detail_values_are_discarded(details):
    assert VerificationError('MODEL_INVALID_RESPONSE',details=details).details == {}


@pytest.mark.parametrize('stage,index',[('generation',1),('verification',1),('generation',2),('verification',2)])
def test_verifier_attaches_actual_stage_and_one_based_call_index(stage,index):
    error=VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED',details={
        'stage':'verification' if stage=='generation' else 'generation','call_index':9,
        'input_bound':13000,'input_limit':12000})
    generated = [answer()]*(index-1)+([error] if stage=='generation' else [answer()])
    checked = [verdict(complete=False)]*(index-1)+([error] if stage=='verification' else [])
    generator,checker=Model(generated),Model(checked)
    with pytest.raises(VerificationError) as caught:
        AnswerVerifier(generator,checker).answer('条件？',SOURCES,deadline=time.monotonic()+10)
    assert caught.value.code=='MODEL_TOKEN_BUDGET_EXCEEDED'
    assert caught.value.details=={'stage':stage,'call_index':index,'input_bound':13000,'input_limit':12000}
    assert len(generator.calls)==index
    assert len(checker.calls)==index-(stage=='generation')


@pytest.mark.parametrize('stage',['generation','verification'])
def test_schema_errors_also_identify_the_failing_stage(stage):
    generator=Model([{} if stage=='generation' else answer()])
    checker=Model([{}] if stage=='verification' else [])
    with pytest.raises(VerificationError) as caught:
        AnswerVerifier(generator,checker).answer('条件？',SOURCES,deadline=time.monotonic()+10)
    assert caught.value.details['stage']==stage
    assert caught.value.details['call_index']==1


@pytest.mark.parametrize('failure_kind',['input_budget','context_budget'])
def test_input_budget_failure_records_safe_bounds_before_http(monkeypatch,failure_kind):
    monkeypatch.setenv('SAFE_DIAGNOSTIC_KEY','private-secret')
    settings=LearningSettings(chat_api_key_env='SAFE_DIAGNOSTIC_KEY')
    if failure_kind=='context_budget': settings=replace(settings,context_budget_tokens=200)
    calls=[]
    with httpx.Client(transport=httpx.MockTransport(lambda request:calls.append(request))) as client:
        model=BudgetedJsonModel(settings,client=client,
            max_input_tokens=10 if failure_kind=='input_budget' else 12000,max_output_tokens=100)
        with pytest.raises(VerificationError) as caught:
            model.generate_json([{'role':'user','content':'private-prompt'}],deadline=time.monotonic()+10)
    assert caught.value.code=='MODEL_TOKEN_BUDGET_EXCEEDED' and calls==[]
    details=caught.value.details
    assert details['failure_kind']==failure_kind
    assert details['input_bound']>10
    assert details['input_limit']==model.max_input_tokens
    assert details['output_limit']==100 and details['context_limit']==settings.context_budget_tokens
    assert 'private' not in json.dumps(details)


@pytest.mark.parametrize('finish,content,usage,kind,expected_finish',[
    ('length','private truncated body',{'prompt_tokens':42,'completion_tokens':2000},'finish_reason','length'),
    ('private-provider-secret','private body',{'prompt_tokens':42},'finish_reason',None),
    ('stop','private invalid JSON',{'prompt_tokens':42},'content_json','stop'),
    ('stop','[]',{'prompt_tokens':42},'content_shape','stop'),
    ('stop','{}',{'prompt_tokens':42,'completion_tokens':2001},'output_budget','stop'),
    ('stop','{}',{'prompt_tokens':True,'completion_tokens':2},'usage_invalid','stop'),
])
def test_invalid_response_records_enums_and_valid_usage_without_content(
        monkeypatch,finish,content,usage,kind,expected_finish):
    monkeypatch.setenv('SAFE_DIAGNOSTIC_KEY','private-secret')
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'id':'private-request-id','choices':[{
            'finish_reason':finish,'message':{'role':'assistant','content':content}}],'usage':usage})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model=BudgetedJsonModel(LearningSettings(chat_api_key_env='SAFE_DIAGNOSTIC_KEY'),client=client)
        with pytest.raises(VerificationError) as caught:
            model.generate_json([{'role':'user','content':'private-prompt'}],deadline=time.monotonic()+10)
    assert caught.value.code=='MODEL_INVALID_RESPONSE' and len(calls)==1
    assert model.last_usage is None
    details=caught.value.details
    assert details['failure_kind']==kind
    assert details.get('finish_reason')==expected_finish
    if type(usage.get('prompt_tokens')) is int: assert details['prompt_tokens']==usage['prompt_tokens']
    assert all(type(value) in (str,int) for value in details.values())
    assert 'private' not in json.dumps(details)


def test_runner_keeps_safe_failure_details_and_all_failed_attempts(evaluation):
    from knowpath_backend.rag_eval.runner import run
    calls=[]
    class Pipeline:
        def __init__(self,mode): self.mode=mode
        def answer(self,*args,**kwargs):
            calls.append(self.mode)
            if self.mode=='a':
                raise VerificationError('MODEL_TOKEN_BUDGET_EXCEEDED',details={
                    'stage':'verification','call_index':2,'input_bound':13000,'input_limit':12000,
                    'prompt':'private-prompt','request_id':'private-id'})
            error=RuntimeError('private-exception')
            error.details={'prompt':'private-details','input_bound':99999}
            raise error
    output=evaluation[0].parent/'diagnostic-results.jsonl'
    records=run(frozen(evaluation),output,pipeline_factory=Pipeline,scope_resolver=resolver)
    assert len(records)==len(calls)==28
    for row in records:
        assert row['response'] is None and not row['service_success']
        assert row['error_details']==({'stage':'verification','call_index':2,
            'input_bound':13000,'input_limit':12000} if row['plugin']=='a' else {})
    assert 'private' not in output.read_text(encoding='utf-8')
