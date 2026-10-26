from types import SimpleNamespace
import httpx
import pytest

from knowpath_backend.core.model_port import SDKModelPort
from knowpath_backend.core.turn import TurnBudgetExceeded, TurnCancelled
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.memory import MemoryManager, MemoryConfig
from knowpath_backend.tools.registry import ToolRegistry
from knowpath_backend.test.web_agent_support import completion, ScriptedChatClient


def api():
    from knowpath_backend.learning.agent_runtime.factory import build_web_agent
    from knowpath_backend.learning.agent_runtime.model import WebModelBudget, BudgetedModelPort, OneShotChatPort
    return SimpleNamespace(build=build_web_agent, Budget=WebModelBudget, Port=BudgetedModelPort, HTTP=OneShotChatPort)


@pytest.fixture
def memory():
    manager = MemoryManager(MemoryConfig(tick_mode='off', conflict_strength='off'), context_policy=WebContextPolicy())
    yield manager
    manager.close()


def registry():
    tools = ToolRegistry()
    for name in ('recall', 'remember'):
        tools.register_function(name, 'Scoped memory operation', lambda query: 'scoped result')
    return tools


def test_factory_real_agent_executes_tools_without_committing(memory):
    client = ScriptedChatClient([completion(tool_calls=[('r', 'recall', {'input': 'past'})]), completion('answer')])
    agent = api().build(LearningSettings(), memory=memory, tool_registry=registry(), model_port=SDKModelPort(client))
    assert agent.run('question') == 'answer'
    assert client.requests[0]['tools'][0]['function']['name'] == 'recall'
    assert client.requests[1]['messages'][-1]['role'] == 'tool'
    assert memory.working.items() == []
    assert memory._turn_counter == 0
    assert agent.workspace is None
    assert agent.execution_policy.strict
    assert agent.hooks == []


def test_factory_rejects_automatic_unscoped_memory_tools(memory):
    with pytest.raises(ValueError, match='scoped'):
        api().build(LearningSettings(), memory=memory, tool_registry=ToolRegistry())


def test_model_budget_charges_failures_and_blocks_next_attempt():
    client = ScriptedChatClient([RuntimeError('transport failed')])
    port = api().Port(SDKModelPort(client), api().Budget(max_calls=1))
    request = dict(model='offline', messages=[dict(role='user', content='q')], max_tokens=20)
    with pytest.raises(RuntimeError): port.complete(request, stream=False)
    with pytest.raises(TurnBudgetExceeded): port.complete(request, stream=False)
    assert len(client.requests) == 1


def test_budget_accounts_tools_before_call():
    client = ScriptedChatClient([])
    port = api().Port(SDKModelPort(client), api().Budget(max_input_tokens=100))
    request = dict(model='offline', messages=[dict(role='user', content='q')], tools=[{'schema': 'x' * 300}], max_tokens=20)
    with pytest.raises(TurnBudgetExceeded): port.complete(request, stream=False)
    assert not client.requests


def test_output_reservation_and_cancel_never_emit_candidate():
    client = ScriptedChatClient([completion('answer')])
    port = api().Port(SDKModelPort(client), api().Budget(max_output_tokens=4, output_per_call=4))
    chunks = []
    with pytest.raises(TurnBudgetExceeded):
        port.complete(dict(model='offline', messages=[], max_tokens=5), stream=False, on_text_chunk=chunks.append)
    assert not client.requests and not chunks
    with pytest.raises(TurnCancelled):
        port.complete(dict(model='offline', messages=[], max_tokens=4), stream=False, should_cancel=lambda: True)
    assert not client.requests


def test_one_shot_http_preserves_protocol_and_never_retries(monkeypatch):
    monkeypatch.setenv('OFFLINE_FACTORY_KEY', 'synthetic-key')
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={'error': 'private upstream detail'})
        return httpx.Response(200, json=completion(tool_calls=[('r', 'recall', {'query': 'q'})]).model_dump(mode='json'))
    settings = LearningSettings(chat_api_key_env='OFFLINE_FACTORY_KEY', chat_base_url='https://synthetic.invalid/v1')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        port = api().HTTP(settings, client=client)
        from knowpath_backend.learning.providers.models import ModelError
        with pytest.raises(ModelError) as error:
            port.complete(dict(model=settings.chat_model, messages=[], tools=[]), stream=False)
        assert 'private' not in str(error.value)
        assert len(requests) == 1
        response = port.complete(dict(model=settings.chat_model, messages=[], tools=[{'type': 'function'}]), stream=False)
    assert response.choices[0].message.tool_calls[0].function.name == 'recall'
    assert response.choices[0].finish_reason == 'tool_calls'
    assert response.usage.prompt_tokens == 8
    assert requests[0].headers['authorization'] == 'Bearer synthetic-key'
    assert str(requests[0].url) == 'https://synthetic.invalid/v1/chat/completions'


def test_budget_cancel_after_transport_discards_text():
    cancelled = []
    class Port:
        def complete(self, request, **kwargs):
            kwargs['on_text_chunk']('private candidate')
            cancelled.append(True)
            return completion('private candidate')
    port = api().Port(Port(), should_cancel=lambda: bool(cancelled))
    chunks = []
    with pytest.raises(TurnCancelled):
        port.complete(dict(model='offline', messages=[], max_tokens=20), stream=False, on_text_chunk=chunks.append)
    assert not chunks
    assert port.calls == 1 and port.output_reserved == 20


def test_provider_output_overrun_does_not_emit():
    client = ScriptedChatClient([completion('answer')])
    port = api().Port(SDKModelPort(client))
    chunks = []
    with pytest.raises(TurnBudgetExceeded):
        port.complete(dict(model='offline', messages=[], max_tokens=1), stream=False, on_text_chunk=chunks.append)
    assert not chunks and len(client.requests) == 1


def test_factory_budget_is_request_local(memory):
    client = ScriptedChatClient([completion('one'), completion('two')])
    budget = api().Budget(max_calls=1)
    first = api().build(LearningSettings(), memory=memory, tool_registry=registry(), model_port=SDKModelPort(client), budget=budget)
    second = api().build(LearningSettings(), memory=memory, tool_registry=registry(), model_port=SDKModelPort(client), budget=budget)
    assert first.run('q') == 'one'
    assert second.run('q') == 'two'
    assert first.model_port.calls == second.model_port.calls == 1


def test_output_per_call_cannot_exceed_aggregate_ceiling():
    with pytest.raises(ValueError, match='output'):
        api().Budget(max_output_tokens=2, output_per_call=3)
