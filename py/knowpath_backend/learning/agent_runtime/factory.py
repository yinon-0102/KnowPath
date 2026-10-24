"""Build the generic Agent with explicit Web resources and no shell runtime."""
from types import SimpleNamespace

from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
from knowpath_backend.core.turn import TurnCommitMode, TurnExecutionPolicy
from knowpath_backend.learning.agent_runtime.context import WebContextPolicy
from knowpath_backend.learning.agent_runtime.model import (
    BudgetedModelPort, OneShotChatPort, WebModelBudget,
)


def build_web_agent(settings, *, memory, tool_registry, model_port=None, budget=None,
                    system_prompt=None, should_cancel=None):
    """The caller owns the isolated memory session and scoped tool implementations.

    Registration is an explicit trust boundary: the factory cannot infer whether
    a caller's remember/recall tools enforce scope. It never supplies generic ones.
    Each factory invocation creates a fresh budget; do not reuse across requests.
    """
    if not {'recall', 'remember'} <= set(tool_registry.list_tools()):
        raise ValueError('Caller must register scoped recall and remember tools')
    if not isinstance(memory.context_policy, WebContextPolicy):
        raise ValueError('Web memory requires WebContextPolicy')
    budget = budget or WebModelBudget()
    port = BudgetedModelPort(model_port if model_port is not None else OneShotChatPort(settings),
                             budget, should_cancel=should_cancel)
    llm = SimpleNamespace(provider=settings.chat_provider, model=settings.chat_model,
                          client=None, temperature=0, max_tokens=budget.output_per_call)
    return MyFunctionCallAgent('web', llm, tool_registry, memory=memory,
        system_prompt=system_prompt, model_port=port, model_stream=False, hooks=[],
        workspace=None, max_steps=budget.max_calls, enable_verify=False, enable_tdd=False,
        commit_mode=TurnCommitMode.EXTERNAL, execution_policy=TurnExecutionPolicy.web())
