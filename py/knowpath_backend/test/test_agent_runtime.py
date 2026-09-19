"""Reusable Agent assembly must work without terminal UI dependencies."""

from types import SimpleNamespace

from knowpath_backend.core import runtime
from knowpath_backend.core.permissions import PermissionDecision, PermissionGrants, decide


def test_build_agent_keeps_memory_workspace_and_tools(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_CHAT_STORAGE", str(tmp_path / "memory"))
    monkeypatch.setattr(runtime, "MyLLM", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(runtime, "MyFunctionCallAgent", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(runtime, "MemoryManager", lambda config, **kwargs: SimpleNamespace(config=config, **kwargs))
    monkeypatch.setattr(runtime, "LLMSummarizer", lambda llm, **kwargs: SimpleNamespace(llm=llm))
    cfg = runtime._merge_defaults({"provider": "openai", "model": "test-model", "api_key": "test-key", "workspace": str(tmp_path)})

    agent = runtime.build_agent(cfg)

    assert agent is not None
    assert agent.workspace.root == tmp_path.resolve()
    assert agent.memory.config.context_budget_tokens >= 1024
    assert agent.memory.config.user_storage_dir is not None
    assert agent.memory.summarizer.llm is agent.llm
    assert agent.enable_verify is True
    assert agent.todo_store is not None
    for name in ("Read", "Edit", "Write", "LS", "Grep", "Glob", "Bash", "write_todo"):
        assert agent.tool_registry.get_tool(name) is not None, name


def test_benchmark_factory_uses_noninteractive_runtime(monkeypatch):
    from knowpath_backend.bench.default_factory import build_factory
    monkeypatch.setattr(runtime, "load_config", lambda: {"model": "test"})
    monkeypatch.setattr(runtime, "build_agent", lambda cfg: cfg)
    result = build_factory()("test-workspace")
    assert result == {"model": "test", "workspace": "test-workspace"}


def test_real_agent_assembly_releases_replaced_memory(monkeypatch, tmp_path):
    import sqlite3
    import pytest

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_CHAT_STORAGE", str(tmp_path / "memory"))
    monkeypatch.setattr(runtime, "MyLLM", lambda **kwargs: SimpleNamespace(**kwargs))
    cfg = runtime._merge_defaults({"provider": "openai", "model": "test-model",
                                   "api_key": "test-key", "workspace": str(tmp_path)})
    agent = runtime.build_agent(cfg)
    assert agent is not None
    connection = agent.memory.cold.backend.conn
    agent.memory.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_permission_callback_can_be_provided_without_a_terminal():
    grants = PermissionGrants()
    calls = []

    def approve(name, args, preview):
        calls.append((name, args, preview))
        return PermissionDecision.ALLOW_ALWAYS

    assert decide(grants, approve, "Edit", {"path": "src/a.py"}, "preview")
    assert decide(grants, approve, "Edit", {"path": "src/b.py"}, "preview")
    assert len(calls) == 1
