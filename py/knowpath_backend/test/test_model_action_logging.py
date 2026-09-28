"""Correlated model/tool actions must not change execution or expose payloads."""
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
import logging
import time

import httpx
import pytest

from knowpath_backend.observability import bind_context, context_fields, span


def records(caplog, event):
    return [r.fields for r in caplog.records if getattr(r, 'event', None) == event]


def test_rag_thread_inherits_only_logging_context():
    from knowpath_backend.learning.rag.runtime import ConfiguredPipeline
    transaction = ContextVar('test_database_session', default=None)
    token = transaction.set(object())
    def execute(identifier):
        pipeline = object.__new__(ConfiguredPipeline)
        pipeline._answer = lambda question, **kwargs: {'observed': context_fields(), 'transaction': transaction.get()}
        with bind_context(request_id=identifier, run_id='run-' + identifier, job_id='job-' + identifier):
            result = pipeline.answer('PRIVATE_QUESTION')
            assert result['observed']['request_id'] == identifier
            assert result['observed']['run_id'] == 'run-' + identifier
            assert result['observed']['job_id'] == 'job-' + identifier
            assert result['transaction'] is None
        assert context_fields() == {}
    try:
        execute('direct')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(execute, ['one', 'two']))
    finally:
        transaction.reset(token)


def test_nested_spans_have_distinct_ids_and_restore_parent(caplog):
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger(__name__)
    with bind_context(request_id='request'):
        with span(logger, 'outer'):
            parent = context_fields()['span_id']
            with span(logger, 'inner'):
                assert context_fields()['parent_span_id'] == parent
                assert context_fields()['span_id'] != parent
            assert context_fields()['span_id'] == parent
    assert context_fields() == {}
    inner, = records(caplog, 'inner.completed')
    outer, = records(caplog, 'outer.completed')
    assert inner['parent_span_id'] == outer['span_id']


def test_provider_physical_attempts_have_unique_ids_and_safe_usage(caplog, monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.providers.models import DashScopeChatAdapter
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'PRIVATE_KEY')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json={
            'choices':[{'finish_reason':'stop','message':{'content':'PRIVATE_ANSWER'}}],
            'usage':{'prompt_tokens':12,'completion_tokens':3,'total_tokens':15,'secret':'PRIVATE_USAGE'}})
    with bind_context(request_id='request'), httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert DashScopeChatAdapter(LearningSettings(), client=client).generate([{'role':'user','content':'PRIVATE_QUESTION'}])['content'] == 'PRIVATE_ANSWER'
    started = records(caplog, 'model.call.started')
    assert len(started) == 2 and len({r['model_call_id'] for r in started}) == 2
    assert started[0]['parent_span_id'] == started[1]['parent_span_id']
    complete, = records(caplog, 'model.call.completed')
    failure, = records(caplog, 'model.call.failed')
    assert failure['status_code'] == 429 and failure['attempt'] == 1
    assert complete['attempt'] == 2 and complete['usage']['prompt_tokens'] == 12
    assert complete['usage_status'] == 'observed' and complete['request_id'] == 'request'
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r, 'fields'))


def test_journal_events_keep_context_ids_and_ignore_late_completion(caplog):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    caplog.set_level(logging.DEBUG)
    with bind_context(request_id='request', run_id='run', span_id='parent'):
        journal = RequestJournal()
        first = journal.begin_call('generation')
        journal.finish_call(first, status='succeeded', usage={'prompt_tokens':8}, http_status=200)
        second = journal.begin_call('verification')
    snapshot = journal.seal('deadline')
    journal.finish_call(second, status='succeeded', usage={'prompt_tokens':100})
    assert journal.snapshot() == snapshot
    completed, = records(caplog, 'model.call.completed')
    unknown, = records(caplog, 'model.call.unknown')
    assert completed['request_id'] == unknown['request_id'] == 'request'
    assert completed['call_index'] == 1 and unknown['call_index'] == 2
    assert snapshot['calls'][0]['model_call_id'] == completed['model_call_id']
    assert snapshot['calls'][1]['model_call_id'] == unknown['model_call_id']
    assert unknown['usage_status'] == 'unknown'


@pytest.mark.parametrize('journaled', [False, True])
def test_budgeted_model_emits_one_call_with_usage_and_context(caplog, monkeypatch, journaled):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
    from knowpath_backend.learning.rag.runtime import DeadlineTransport
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'PRIVATE_KEY')
    session = ContextVar('private_sql_session', default=None)
    seen = []
    def handle(request):
        seen.append((context_fields(), session.get()))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"ok":true}'}}],
            'usage':{'prompt_tokens':20,'completion_tokens':5}})
    journal = RequestJournal() if journaled else None
    if journaled:
        monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kw: httpx.MockTransport(handle))
    transport = DeadlineTransport(time.monotonic()+120, journal=journal) if journaled else httpx.MockTransport(handle)
    with bind_context(request_id='request', run_id='run'), httpx.Client(transport=transport) as client:
        token = session.set('must-not-cross-thread')
        try:
            result = BudgetedJsonModel(LearningSettings(),client=client,journal=journal).generate_json(
                [{'role':'user','content':'PRIVATE_QUESTION'}], deadline=time.monotonic()+120)
        finally:
            session.reset(token)
    assert result == {'ok':True}
    complete, = records(caplog, 'model.call.completed')
    assert complete['request_id'] == 'request' and complete['usage']['prompt_tokens'] == 20
    assert complete['stage'] == 'generation' and complete['model'] == 'qwen-plus'
    assert len(records(caplog, 'model.call.started')) == 1
    assert seen[0][0]['request_id'] == 'request'
    if journaled:
        assert seen[0][1] is None
        validation = records(caplog, 'model.call.validation')
        assert validation[-1]['model_call_id'] == complete['model_call_id']
        assert journal.snapshot()['calls'][0]['model_call_id'] == complete['model_call_id']


def test_numeric_usage_is_visible_but_untrusted_prompt_counter_is_redacted():
    from knowpath_backend.observability.events import safe_value
    assert safe_value({'prompt_tokens':3}) == {'prompt_tokens':3}
    assert safe_value({'prompt_tokens':'PRIVATE_PAYLOAD'}) == {'prompt_tokens':'[REDACTED]'}


def test_streamed_adapter_records_observed_usage_and_finish(caplog, monkeypatch):
    import json
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.providers.models import DashScopeChatAdapter
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv('DASHSCOPE_API_KEY','PRIVATE_KEY')
    chunks = [{'choices':[{'delta':{'content':'PRIVATE_ANSWER'},'finish_reason':'stop'}]},
              {'choices':[],'usage':{'prompt_tokens':9,'completion_tokens':2}}]
    body = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,text=body))) as client:
        assert ''.join(DashScopeChatAdapter(LearningSettings(),client=client).stream([])) == 'PRIVATE_ANSWER'
    row, = records(caplog, 'model.call.completed')
    assert row['usage']['completion_tokens'] == 2 and row['finish_reason'] == 'stop'
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r, 'fields'))


@pytest.mark.parametrize('mode', ['returned', 'failed', 'rejected', 'timed_out', 'parallel'])
def test_agent_tool_actions_preserve_outcomes_and_hide_payload(caplog, mode):
    from types import SimpleNamespace
    from threading import Event
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    from knowpath_backend.tools.registry import ToolRegistry
    from knowpath_backend.tools.base import Tool
    caplog.set_level(logging.DEBUG)
    release, finished = Event(), Event()
    seen = []
    class SampleTool(Tool):
        side_effect_free = mode == 'parallel'
        requires_approval = mode == 'rejected'
        def __init__(self): super().__init__('sample_tool','test')
        def get_parameters(self): return []
        def run(self, params):
            seen.append(context_fields())
            try:
                if mode == 'failed': raise RuntimeError('PRIVATE_FAILURE')
                if mode == 'timed_out': release.wait(2)
                return 'PRIVATE_OUTPUT'
            finally:
                finished.set()
    agent = object.__new__(MyFunctionCallAgent)
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_tool(SampleTool())
    agent.tool_timeout = .02 if mode == 'timed_out' else None
    agent.todo_store = None
    calls = [SimpleNamespace(id='untrusted-provider-id',function=SimpleNamespace(name='sample_tool',arguments='{"value":"PRIVATE_ARGUMENT"}'))]
    messages = []
    try:
        with bind_context(request_id='request', model_call_id='origin-model', span_id='parent'):
            if mode == 'failed':
                with pytest.raises(RuntimeError, match='PRIVATE_FAILURE'):
                    agent._execute_tool_calls(calls,messages,on_tool_call=None,on_permission_request=None,on_tool_result=None)
            else:
                assert agent._execute_tool_calls(calls,messages,on_tool_call=None,
                    on_permission_request=lambda *a:False,on_tool_result=None) == 1
        outcome = {'returned':'completed','parallel':'completed','failed':'failed','rejected':'rejected','timed_out':'timed_out'}[mode]
        row, = records(caplog, 'tool.call.' + outcome)
        assert row['request_id'] == 'request' and row['origin_model_call_id'] == 'origin-model'
        assert row['tool_name'] == 'sample_tool' and row['tool_call_id']
        if mode not in {'failed','rejected','timed_out'}:
            assert messages[0]['content'] == 'PRIVATE_OUTPUT'
        if mode == 'rejected': assert not seen
        else: assert seen[0]['request_id'] == 'request'
        assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r,'fields'))
    finally:
        release.set()
        if mode != 'rejected': assert finished.wait(1)
    if mode == 'timed_out':
        assert not records(caplog, 'tool.call.completed')


@pytest.mark.parametrize('cancel', [False, True])
def test_agent_model_stream_has_safe_usage_and_cancellation(caplog, cancel):
    from types import SimpleNamespace as NS
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    caplog.set_level(logging.INFO)
    chunks = [NS(choices=[NS(delta=NS(content='PRIVATE_ANSWER'), finish_reason='stop')]),
              NS(choices=[], usage=NS(prompt_tokens=8, completion_tokens=2))]
    client = NS(chat=NS(completions=NS(create=lambda **kwargs: iter(chunks))))
    with bind_context(request_id='request', span_id='parent'):
        result = MyFunctionCallAgent._stream_chat_completion(client, {'model':'test-model'}, None,
                                                            should_cancel=lambda: cancel)
    event = 'model.call.cancelled' if cancel else 'model.call.completed'
    row, = records(caplog, event)
    started, = records(caplog, 'model.call.started')
    assert row['model_call_id'] == started['model_call_id'] == result._model_call_id
    assert row['parent_span_id'] == 'parent' and row['observation_scope'] == 'sdk'
    if not cancel:
        assert row['usage']['prompt_tokens'] == 8 and row['finish_reason'] == 'stop'
        assert result.choices[0].message.content == 'PRIVATE_ANSWER'
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r,'fields'))


@pytest.mark.parametrize('mode', ['invoke', 'stream', 'think_stream', 'anthropic', 'gemini'])
def test_core_model_sdk_paths_record_usage(caplog, mode):
    from types import SimpleNamespace as NS
    from knowpath_backend.core.llm import MyLLM
    caplog.set_level(logging.INFO)
    llm = object.__new__(MyLLM)
    llm.model, llm.temperature, llm.max_tokens = 'test-model', .5, 100
    llm.provider = mode if mode in {'anthropic', 'gemini'} else 'openai'
    response = NS(choices=[NS(message=NS(content='PRIVATE_ANSWER'), finish_reason='stop')],
                  usage=NS(prompt_tokens=8, completion_tokens=2))
    chunks = [NS(choices=[NS(delta=NS(content='PRIVATE_ANSWER'), finish_reason='stop')]),
              NS(choices=[], usage=response.usage)]
    if mode == 'anthropic':
        response = NS(content=[NS(type='text',text='PRIVATE_ANSWER')], usage=NS(input_tokens=8,output_tokens=2))
        llm.client = NS(messages=NS(create=lambda **kwargs:response))
    elif mode == 'gemini':
        response = NS(text='PRIVATE_ANSWER',usage_metadata=NS(prompt_token_count=8,candidates_token_count=2,total_token_count=10))
        llm.client = NS(models=NS(generate_content=lambda **kwargs:response))
    else:
        llm.client = NS(chat=NS(completions=NS(create=lambda **kwargs:iter(chunks) if kwargs.get('stream') else response)))
    messages = [{'role':'user','content':'PRIVATE_QUESTION'}]
    with bind_context(request_id='request'):
        result = ''.join(llm.stream_invoke(messages)) if mode == 'stream' else llm.think(messages,stream=mode == 'think_stream')
    assert result == 'PRIVATE_ANSWER'
    row, = records(caplog, 'model.call.completed')
    assert row['usage_status'] == 'observed' and row['request_id'] == 'request'
    assert row['observation_scope'] == 'sdk'
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r,'fields'))


@pytest.mark.parametrize('stage', ['embedding', 'reranking'])
def test_retrieval_model_actions_and_preflight_rejection(caplog, monkeypatch, stage):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.retrieval import DashScopeEmbedder, RetrievalError
    from knowpath_backend.learning.rag.reranking import DashScopeReranker
    caplog.set_level(logging.INFO)
    monkeypatch.setenv('DASHSCOPE_API_KEY','PRIVATE_KEY')
    payload = {'data':[{'index':0,'embedding':[.1]*1024}], 'output':{'results':[{'index':0,'relevance_score':.5}]},
               'usage':{'input_tokens':8,'total_tokens':8}}
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))) as client:
        model = DashScopeEmbedder(LearningSettings(),client=client) if stage == 'embedding' else DashScopeReranker(LearningSettings(),client=client)
        with bind_context(request_id='request'):
            if stage == 'embedding': model.embed(['PRIVATE_TEXT'])
            else: model.rerank('PRIVATE_QUESTION',[{'retrieval_text':'PRIVATE_TEXT'}],deadline=time.monotonic()+60)
        row, = records(caplog,'model.call.completed')
        assert row['stage'] == stage and row['usage']['input_tokens'] == 8
        assert row['request_id'] == 'request'
        caplog.clear()
        with pytest.raises(RetrievalError):
            if stage == 'embedding': model.embed([''])
            else: model.rerank('',[{'retrieval_text':'x'}],deadline=time.monotonic()+60)
        assert not records(caplog,'model.call.started')


def test_direct_tool_registry_actions_and_dependency_default(caplog):
    from knowpath_backend.tools.registry import ToolRegistry
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    caplog.set_level(logging.INFO)
    registry = ToolRegistry()
    registry.register_function('sample','test',lambda arg:'PRIVATE_OUTPUT')
    assert registry.execute_tool('sample',{'input':'PRIVATE_ARG'}) == 'PRIVATE_OUTPUT'
    started, = records(caplog,'tool.call.started')
    completed, = records(caplog,'tool.call.completed')
    assert started['tool_call_id'] == completed['tool_call_id']
    journal = RequestJournal()
    identifier = journal.begin_call('retrieval')
    journal.finish_call(identifier,status='succeeded')
    assert not records(caplog,'model.call.started')


def test_revision_metadata_reaches_each_model_and_journal():
    from knowpath_backend.test.test_rag_verification import Model, answer, verdict, SOURCES
    from knowpath_backend.learning.rag.verification import AnswerVerifier
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    seen = []
    journal = RequestJournal()
    class ObservedModel(Model):
        def __init__(self, stage, results):
            super().__init__(results)
            self.stage = stage
        def generate_json(self, messages, *, deadline):
            seen.append(context_fields())
            identifier = journal.begin_call(self.stage)
            journal.finish_call(identifier, status='succeeded')
            return super().generate_json(messages,deadline=deadline)
    generator = ObservedModel('generation',[answer('所有人都能申请。'),answer()])
    checker = ObservedModel('verification',[verdict('unsupported'),verdict()])
    result = AnswerVerifier(generator,checker).answer('谁能申请？',SOURCES,deadline=time.monotonic()+60)
    assert result['status'] == 'answered'
    assert [row['revision_index'] for row in seen] == [0,0,1,1]
    assert [row['stage_call_index'] for row in seen] == [1,1,2,2]
    assert [row['revision_index'] for row in journal.snapshot()['calls']] == [0,0,1,1]
    assert 'revision_index' not in context_fields()


def test_aborted_tool_batch_finishes_unexecuted_actions(caplog):
    from types import SimpleNamespace as NS
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    from knowpath_backend.tools.registry import ToolRegistry
    caplog.set_level(logging.INFO)
    agent = object.__new__(MyFunctionCallAgent)
    agent.tool_registry, agent.tool_timeout, agent.todo_store = ToolRegistry(), None, None
    def failure(arg): raise RuntimeError('PRIVATE_ERROR')
    agent.tool_registry.register_function('fails','test',failure)
    agent.tool_registry.register_function('later','test',lambda arg:pytest.fail('must not execute'))
    calls = [NS(id=name,function=NS(name=name,arguments='{}')) for name in ['fails','later']]
    with pytest.raises(RuntimeError,match='PRIVATE_ERROR'):
        agent._execute_tool_calls(calls,[],on_tool_call=None,on_permission_request=None,on_tool_result=None)
    started = records(caplog,'tool.call.started')
    terminals = records(caplog,'tool.call.failed')+records(caplog,'tool.call.cancelled')
    assert {r['tool_call_id'] for r in started} == {r['tool_call_id'] for r in terminals}
    cancelled, = records(caplog,'tool.call.cancelled')
    assert cancelled['tool_name'] == 'later' and not cancelled['execution_started']


def test_nested_registered_tool_gets_its_own_child_action(caplog):
    from knowpath_backend.tools.registry import ToolRegistry
    caplog.set_level(logging.INFO)
    registry = ToolRegistry()
    registry.register_function('inner','test',lambda arg:'ok')
    registry.register_function('outer','test',lambda arg:registry.execute_tool('inner',arg))
    assert registry.execute_tool('outer','PRIVATE_ARGUMENT') == 'ok'
    rows = records(caplog,'tool.call.completed')
    assert len(rows) == 2
    inner, outer = rows
    assert inner['parent_span_id'] == outer['span_id']
    assert inner['tool_call_id'] != outer['tool_call_id']


def test_agent_budget_retry_emits_distinct_sdk_actions(caplog):
    from types import SimpleNamespace as NS
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    caplog.set_level(logging.INFO)
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return iter([NS(choices=[NS(delta=NS(content='' if len(requests)==1 else 'PRIVATE_OUTPUT'),
                                    finish_reason='length' if len(requests)==1 else 'stop')])])
    agent = object.__new__(MyFunctionCallAgent)
    agent.llm = NS(client=NS(chat=NS(completions=NS(create=create))),model='test-model',
                   provider='openai',temperature=.5,max_tokens=128)
    result = agent._invoke_with_tools([{'role':'user','content':'PRIVATE_QUESTION'}],[],'auto')
    rows = records(caplog,'model.call.completed')
    assert len(rows) == 2 and rows[0]['model_call_id'] != rows[1]['model_call_id']
    assert [r['attempt'] for r in rows] == [1,2]
    assert rows[1]['retry_reason'] == 'empty_length_limit'
    assert result._model_call_id == rows[1]['model_call_id']
    assert [r['max_tokens'] for r in requests] == [128,256]


def test_stream_preflight_does_not_claim_physical_call(caplog, monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.providers.models import DashScopeChatAdapter, ModelError
    caplog.set_level(logging.INFO)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    with pytest.raises(ModelError):
        list(DashScopeChatAdapter(LearningSettings()).stream([]))
    assert not records(caplog,'model.call.started')


def test_parallel_late_exception_cannot_replace_observed_timeout(caplog, monkeypatch):
    import concurrent.futures as futures
    from threading import Event
    from types import SimpleNamespace as NS
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    from knowpath_backend.tools.registry import ToolRegistry
    from knowpath_backend.tools.base import Tool
    caplog.set_level(logging.INFO)
    release = Event()
    class LateFailure(Tool):
        side_effect_free = True
        def __init__(self): super().__init__('late_failure','test')
        def get_parameters(self): return []
        def run(self, args):
            assert release.wait(2)
            raise RuntimeError('PRIVATE_LATE_ERROR')
    real_wait = futures.wait
    def late_wait(fs, **kwargs):
        observed = real_wait(fs, **kwargs)
        assert observed.not_done
        release.set()
        real_wait(fs, timeout=2)
        return observed
    monkeypatch.setattr(futures,'wait',late_wait)
    agent = object.__new__(MyFunctionCallAgent)
    agent.tool_registry, agent.tool_timeout, agent.todo_store = ToolRegistry(), .02, None
    agent.tool_registry.register_tool(LateFailure())
    messages = []
    try:
        assert agent._execute_tool_calls([NS(id='call',function=NS(name='late_failure',arguments='{}'))],
            messages,on_tool_call=None,on_permission_request=None,on_tool_result=None) == 1
    finally:
        release.set()
    assert '超时' in messages[0]['content']
    assert len(records(caplog,'tool.call.timed_out')) == 1
    assert not records(caplog,'tool.call.failed')


def test_agent_run_links_tool_to_originating_model_call(caplog, monkeypatch):
    from types import SimpleNamespace as NS
    from knowpath_backend.test.test_function_call_cancel import _bare_agent
    from knowpath_backend.tools.registry import ToolRegistry
    caplog.set_level(logging.INFO)
    agent = _bare_agent(monkeypatch)
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_function('sample','test',lambda arg:'PRIVATE_RESULT')
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        delta = NS(content='PRIVATE_FINAL') if len(calls)>1 else NS(content=None,
            tool_calls=[NS(index=0,id='provider-id',function=NS(name='sample',arguments='{}'))])
        return iter([NS(choices=[NS(delta=delta,finish_reason='stop' if len(calls)>1 else 'tool_calls')])])
    agent.llm = NS(client=NS(chat=NS(completions=NS(create=create))),model='test-model',
                   provider='openai',temperature=.5,max_tokens=128)
    assert agent.run('PRIVATE_QUESTION') == 'PRIVATE_FINAL'
    models = records(caplog,'model.call.completed')
    tool, = records(caplog,'tool.call.completed')
    assert len(models) == 2
    assert tool['origin_model_call_id'] == models[0]['model_call_id']
    assert tool['parent_span_id'] == models[0]['parent_span_id'] == models[1]['parent_span_id']
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r,'fields'))


@pytest.mark.parametrize('outcome', ['returned_error','raised'])
def test_direct_registry_failure_semantics_are_preserved(caplog, outcome):
    from knowpath_backend.tools.registry import ToolRegistry
    caplog.set_level(logging.INFO)
    registry = ToolRegistry()
    def failure(arg):
        if outcome == 'raised': raise RuntimeError('PRIVATE_EXCEPTION')
        return '❌ PRIVATE_ERROR_RESULT'
    registry.register_function('sample','test',failure)
    if outcome == 'raised':
        with pytest.raises(RuntimeError,match='PRIVATE_EXCEPTION'):
            registry.execute_tool('sample','PRIVATE_ARGUMENT')
        assert len(records(caplog,'tool.call.failed')) == 1
    else:
        assert registry.execute_tool('sample','PRIVATE_ARGUMENT') == '❌ PRIVATE_ERROR_RESULT'
        row, = records(caplog,'tool.call.completed')
        assert row['result_status'] == 'error_reported'
    assert all('PRIVATE_' not in str(r.fields) for r in caplog.records if hasattr(r,'fields'))


def test_submitted_queued_tools_are_unknown_after_batch_abort(caplog, monkeypatch):
    import concurrent.futures as futures
    from threading import Event
    from types import SimpleNamespace as NS
    from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
    from knowpath_backend.tools.registry import ToolRegistry
    from knowpath_backend.tools.base import Tool
    caplog.set_level(logging.INFO)
    release, submitted = Event(), []
    real_as_completed = futures.as_completed
    def observe_futures(fs):
        submitted.extend(fs)
        yield from real_as_completed(fs)
    monkeypatch.setattr(futures,'as_completed',observe_futures)
    class Sample(Tool):
        side_effect_free = True
        def __init__(self,index):
            super().__init__('sample_'+str(index),'test')
            self.index = index
        def get_parameters(self): return []
        def run(self,args):
            if self.index == 0: raise RuntimeError('PRIVATE_FAILURE')
            assert release.wait(5)
            return 'PRIVATE_OUTPUT'
    agent = object.__new__(MyFunctionCallAgent)
    agent.tool_registry, agent.tool_timeout, agent.todo_store = ToolRegistry(), None, None
    for index in range(16): agent.tool_registry.register_tool(Sample(index))
    calls = [NS(id=str(index),function=NS(name='sample_'+str(index),arguments='{}')) for index in range(16)]
    try:
        with pytest.raises(RuntimeError,match='PRIVATE_FAILURE'):
            agent._execute_tool_calls(calls,[],on_tool_call=None,on_permission_request=None,on_tool_result=None)
        assert not records(caplog,'tool.call.cancelled')
        unknown = records(caplog,'tool.call.unknown')
        assert len(unknown) == 15 and all(row['execution_may_continue'] for row in unknown)
        assert any(not row['execution_started'] for row in unknown)
    finally:
        release.set()
        assert not futures.wait(submitted,timeout=5).not_done


@pytest.mark.parametrize('host_configured',[False,True])
def test_migration_logging_respects_existing_application_loggers(monkeypatch, host_configured):
    import logging.config
    import runpy
    from pathlib import Path
    from types import SimpleNamespace as NS
    from alembic import context
    import dotenv
    backend = Path(__file__).resolve().parents[2]
    root = logging.getLogger()
    handlers = [logging.NullHandler()] if host_configured else []
    monkeypatch.setattr(root,'handlers',handlers)
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**kw:False)
    monkeypatch.delenv('DATABASE_URL',raising=False)
    class StopBeforeDatabase(Exception): pass
    def stop(key): raise StopBeforeDatabase()
    monkeypatch.setattr(context,'config',NS(config_file_name=str(backend/'alembic.ini'),get_main_option=stop),raising=False)
    observed = []
    monkeypatch.setattr(logging.config,'fileConfig',lambda *a,**kw:observed.append(kw))
    with pytest.raises(StopBeforeDatabase):
        runpy.run_path(str(backend/'migrations'/'env.py'))
    assert root.handlers is handlers
    assert observed == ([] if host_configured else [{'disable_existing_loggers':False}])
