from types import SimpleNamespace
import pytest
from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
from knowpath_backend.memory import MemoryConfig, MemoryManager
from knowpath_backend.tools.registry import ToolRegistry
from knowpath_backend.test.web_agent_support import ScriptedChatClient, completion


def agent(memory, responses=(), **kwargs):
    client = ScriptedChatClient(responses)
    llm = SimpleNamespace(provider='openai', model='scripted', client=client,
                          temperature=0, max_tokens=64)
    return MyFunctionCallAgent('injected', llm, ToolRegistry(), memory=memory,
                               hooks=[], honesty_contract=False, **kwargs), client


@pytest.fixture
def memory():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'))
    yield manager
    manager.close()


def test_injected_memory_is_used_without_constructing_second(monkeypatch, memory):
    def forbidden(*args, **kwargs):
        raise AssertionError('second manager created')
    monkeypatch.setattr('knowpath_backend.core.agent.MemoryManager', forbidden)
    runtime, _ = agent(memory, [completion('answer')])
    assert runtime.memory is memory
    assert runtime.run('question') == 'answer'
    assert [item.content for item in memory.working.items()] == ['question', 'answer']


def test_external_commit_leaves_history_and_tick_unchanged(memory):
    runtime, _ = agent(memory, [completion('candidate')], commit_mode='external')
    assert runtime.run('question') == 'candidate'
    assert memory.working.items() == []
    assert memory._turn_counter == 0


def test_injected_memory_remains_caller_owned(memory, monkeypatch):
    runtime, _ = agent(memory)
    with monkeypatch.context() as patch:
        patch.setattr(memory, 'close', lambda: pytest.fail('borrowed memory closed'))
        runtime.close()
        runtime.close()


def test_card_store_injection_bypasses_disk_factory(tmp_path, monkeypatch):
    from knowpath_backend.memory.playbook import PlaybookStore
    store = PlaybookStore()
    def forbidden(*args, **kwargs):
        raise AssertionError('disk card store created')
    monkeypatch.setattr('knowpath_backend.memory.manager.PlaybookStore', forbidden)
    manager = MemoryManager(MemoryConfig(storage_dir=tmp_path, tick_mode='off',
                                        conflict_strength='off'), card_store=store)
    assert manager.playbook is store
    manager.close()


@pytest.mark.parametrize('stream', [True, False])
def test_model_port_is_only_physical_call_path(memory, stream):
    from knowpath_backend.core.model_port import SDKModelPort
    client = ScriptedChatClient([completion('from port')])
    runtime, unused = agent(memory, model_port=SDKModelPort(client), model_stream=stream)
    chunks = []
    assert runtime.run('question', on_text_chunk=chunks.append) == 'from port'
    assert ''.join(chunks) == 'from port'
    assert client.requests[0].get('stream', False) is stream
    assert unused.requests == []


def test_strict_cancel_has_no_commit_or_call(memory):
    from knowpath_backend.core.turn import TurnCancelled, TurnExecutionPolicy
    runtime, client = agent(memory, execution_policy=TurnExecutionPolicy.web(), commit_mode='external')
    with pytest.raises(TurnCancelled):
        runtime.run('question', should_cancel=lambda: True)
    assert client.requests == []
    assert memory.working.items() == []


@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_strict_empty_has_no_retry_or_reasoning_fallback(memory, finish):
    from knowpath_backend.core.turn import EmptyTurnResponse, TurnExecutionPolicy
    runtime, client = agent(memory, [completion('', finish_reason=finish)],
                            execution_policy=TurnExecutionPolicy.web())
    with pytest.raises(EmptyTurnResponse):
        runtime.run('question')
    assert len(client.requests) == 1
    assert memory.working.items() == []


def test_strict_step_exhaustion_has_no_fallback(memory):
    from knowpath_backend.core.turn import TurnBudgetExceeded, TurnExecutionPolicy
    runtime, client = agent(memory, max_steps=0, execution_policy=TurnExecutionPolicy.web())
    with pytest.raises(TurnBudgetExceeded):
        runtime.run('question')
    assert client.requests == []


def test_strict_callback_failure_propagates_without_commit(memory):
    from knowpath_backend.core.turn import TurnExecutionPolicy
    runtime, client = agent(memory, [completion('answer')], execution_policy=TurnExecutionPolicy.web())
    def fatal(_):
        raise RuntimeError('lease lost')
    with pytest.raises(RuntimeError, match='lease lost'):
        runtime.run('question', on_text_chunk=fatal)
    assert len(client.requests) == 1
    assert memory.working.items() == []


def test_port_failure_does_not_finalize(memory):
    class Port:
        def complete(self, *args, **kwargs):
            raise RuntimeError('budget exhausted')
    runtime, client = agent(memory, model_port=Port())
    with pytest.raises(RuntimeError, match='budget exhausted'):
        runtime.run('question')
    assert client.requests == []
    assert memory.working.items() == []


@pytest.mark.parametrize('callback_name', ['on_llm_done', 'on_tool_call', 'on_tool_result'])
def test_strict_tool_callbacks_abort_without_another_call(memory, callback_name):
    from knowpath_backend.core.turn import TurnExecutionPolicy
    runtime, client = agent(memory, [completion(tool_calls=[('c', 'recall', {'query': 'q'})])],
                            execution_policy=TurnExecutionPolicy.web())
    def fatal(*args):
        raise RuntimeError('lease lost')
    with pytest.raises(RuntimeError, match='lease lost'):
        runtime.run('question', **{callback_name: fatal})
    assert len(client.requests) == 1
    assert memory.working.items() == []


def test_strict_cancel_during_stream_does_not_commit(memory):
    from knowpath_backend.core.turn import TurnCancelled, TurnExecutionPolicy
    cancelled = []
    runtime, client = agent(memory, [completion('partial')], execution_policy=TurnExecutionPolicy.web())
    with pytest.raises(TurnCancelled):
        runtime.run('question', on_text_chunk=lambda text: cancelled.append(True),
                    should_cancel=lambda: bool(cancelled))
    assert len(client.requests) == 1
    assert memory.working.items() == []


def test_legacy_stream_cancellation_is_not_persisted_even_if_signal_resets(memory):
    signals = iter([False, False, False, True])
    runtime, _ = agent(memory, [completion('partial')])
    runtime.run('question', should_cancel=lambda: next(signals, False))
    assert memory.working.items() == []


def test_strict_cancel_precedes_context_assembly(memory, monkeypatch):
    from knowpath_backend.core.turn import TurnCancelled, TurnExecutionPolicy
    monkeypatch.setattr(memory, 'assemble_context', lambda *args, **kwargs: pytest.fail('context touched'))
    runtime, _ = agent(memory, execution_policy=TurnExecutionPolicy.web())
    with pytest.raises(TurnCancelled):
        runtime.run('question', should_cancel=lambda: True)


def test_stream_is_closed_on_callback_failure(memory):
    from knowpath_backend.core.turn import TurnExecutionPolicy
    class ClosableStream:
        closed = False
        def __iter__(self):
            return iter(ScriptedChatClient._chunks(completion('answer')))
        def close(self):
            self.closed = True
    stream = ClosableStream()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: stream)))
    from knowpath_backend.core.model_port import SDKModelPort
    runtime, _ = agent(memory, model_port=SDKModelPort(client), execution_policy=TurnExecutionPolicy.web())
    def fail(text):
        raise RuntimeError('lease lost')
    with pytest.raises(RuntimeError, match='lease lost'):
        runtime.run('q', on_text_chunk=fail)
    assert stream.closed


def test_default_standalone_constructor_still_finalizes(tmp_path):
    from knowpath_backend.test.web_agent_support import make_protocol_agent
    runtime = make_protocol_agent(tmp_path, ScriptedChatClient([completion('answer')]))
    assert runtime.run('question') == 'answer'
    assert [item.content for item in runtime.memory.working.items()] == ['question', 'answer']
    runtime.close()


def test_context_policy_is_used(memory):
    def context_policy(memory, system_prompt, query):
        return [{'role': 'system', 'content': 'trusted'},
                {'role': 'user', 'content': 'untrusted memory'}]
    runtime, client = agent(memory, [completion('answer')], context_policy=context_policy)
    runtime.run('question')
    assert client.requests[0]['messages'][1]['role'] == 'user'


def test_versioned_contracts_roundtrip_and_reject_unknown_versions():
    from knowpath_backend.learning.agent_runtime.contracts import TurnRequest, TurnBudgetLimits
    request = TurnRequest(message_id='m', run_id='r', space_id='s', conversation_id='c',
                          input_text='question', budget=TurnBudgetLimits())
    assert TurnRequest.from_dict(request.to_dict()) == request
    payload = request.to_dict()
    payload['schema_version'] = 999
    with pytest.raises(ValueError, match='version'):
        TurnRequest.from_dict(payload)


def test_foundation_does_not_import_learning():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1]
    for folder in ('core', 'memory'):
        for path in (root / folder).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                         [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                assert not any('knowpath_backend.learning' in name for name in names), path

@pytest.mark.parametrize('stream', [True, False])
def test_injected_port_preserves_tool_calls_usage_and_finish(memory, stream):
    from knowpath_backend.core.model_port import SDKModelPort
    client = ScriptedChatClient([
        completion(tool_calls=[('remember-id', 'remember', {'content': 'prefers examples'})]),
        completion('remembered')])
    runtime, unused = agent(memory, model_port=SDKModelPort(client), model_stream=stream,
                            commit_mode='external')
    usage = []
    assert runtime.run('remember this', on_llm_done=lambda *args: usage.append(args[1:])) == 'remembered'
    assert usage == [(8, 4), (8, 4)]
    assert runtime.last_tool_call_count == 1
    assert client.requests[1]['messages'][-1]['tool_call_id'] == 'remember-id'
    assert memory.playbook.all_active()
    assert memory.working.items() == []
    assert unused.requests == []


def test_strict_real_tool_budget_exhaustion_does_not_make_fallback_call(memory):
    from knowpath_backend.core.turn import TurnBudgetExceeded, TurnExecutionPolicy
    runtime, client = agent(memory, [completion(tool_calls=[('c', 'recall', {'query': 'q'})])],
                            max_steps=1, execution_policy=TurnExecutionPolicy.web())
    with pytest.raises(TurnBudgetExceeded):
        runtime.run('q')
    assert len(client.requests) == 1
    assert memory.working.items() == []


def test_memory_candidate_contract_roundtrip_and_no_callable_snapshot():
    from knowpath_backend.memory.contracts import MemoryCheckpoint, MemoryMutation
    from knowpath_backend.learning.agent_runtime.contracts import TurnCandidate, TurnRequest
    checkpoint = MemoryCheckpoint(revision=2, state={'l1': ['history']})
    mutation = MemoryMutation(expected_revision=2, checkpoint=checkpoint)
    candidate = TurnCandidate(text='answer', memory_mutation=mutation)
    assert TurnCandidate.from_dict(candidate.to_dict()) == candidate
    with pytest.raises(ValueError, match='version'):
        MemoryCheckpoint.from_dict(dict(checkpoint.to_dict(), schema_version=55))
    request = TurnRequest('m', 'r', 's', 'c', 'q', material_bindings=[{'client': lambda: None}])
    with pytest.raises(TypeError):
        request.to_dict()


def test_strict_response_hook_cannot_finalize_blank_answer(memory):
    from knowpath_backend.core.turn import EmptyTurnResponse, TurnExecutionPolicy
    class BlankHook:
        def on_response(self, runtime, user_input, response, messages):
            return '   '
    runtime, _ = agent(memory, [completion('answer')], execution_policy=TurnExecutionPolicy.web())
    runtime.hooks = [BlankHook()]
    with pytest.raises(EmptyTurnResponse):
        runtime.run('question')
    assert memory.working.items() == []
    assert memory._turn_counter == 0


def test_legacy_cancel_between_loop_and_physical_call_returns_interruption(memory):
    signals = iter([False, True])
    runtime, client = agent(memory, [completion('unused')])
    result = runtime.run('question', should_cancel=lambda: next(signals, True))
    assert '中断' in result
    assert client.requests == []
    assert memory.working.items() == []


def test_strict_default_hooks_cannot_bypass_injected_port(memory):
    from knowpath_backend.core.turn import TurnExecutionPolicy
    from knowpath_backend.core.model_port import SDKModelPort
    bypass_calls = []
    from knowpath_backend.memory.item import MemoryItem
    memory.semantic.add(MemoryItem(content='Alice likes examples', role='user'))
    client = ScriptedChatClient([completion('[NEEDS_RECALL: Alice]')])
    llm = SimpleNamespace(provider='openai', model='scripted', client=None,
                          temperature=0, max_tokens=64,
                          invoke=lambda *args, **kwargs: bypass_calls.append(True) or 'bypassed')
    runtime = MyFunctionCallAgent('strict', llm, ToolRegistry(), memory=memory,
                                 model_port=SDKModelPort(client), honesty_contract=False,
                                 execution_policy=TurnExecutionPolicy.web())
    assert runtime.run('question') == '[NEEDS_RECALL: Alice]'
    assert bypass_calls == []
    assert runtime.hooks == []


@pytest.mark.parametrize('option', ['enable_verify', 'enable_tdd'])
def test_strict_rejects_unbudgeted_sdk_execution_modes(memory, option):
    from knowpath_backend.core.turn import TurnExecutionPolicy
    with pytest.raises(ValueError, match='strict'):
        agent(memory, execution_policy=TurnExecutionPolicy.web(), **{option: True})


@pytest.mark.parametrize('option', ['enable_verify', 'enable_tdd'])
def test_invalid_strict_configuration_is_rejected_before_creating_owned_memory(monkeypatch, option):
    from knowpath_backend.core.turn import TurnExecutionPolicy

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid configuration must not construct memory stores')

    monkeypatch.setattr('knowpath_backend.core.agent.MemoryManager', forbidden)
    with pytest.raises(ValueError, match='strict'):
        agent(None, execution_policy=TurnExecutionPolicy.web(), **{option: True})


def test_legacy_transient_cancel_at_finalization_never_commits(memory):
    finalizing = []
    samples = iter([True, False])
    class Hook:
        def on_response(self, *args):
            finalizing.append(True)
    runtime, _ = agent(memory, [completion('answer')])
    runtime.hooks = [Hook()]
    runtime.run('question', should_cancel=lambda: next(samples, False) if finalizing else False)
    assert memory.working.items() == []
