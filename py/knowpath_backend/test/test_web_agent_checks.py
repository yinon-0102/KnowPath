"""The Agent regression runner must isolate resources before importing the app."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


def runner():
    path = BACKEND / "scripts/run_agent_checks.py"
    assert path.exists(), "Agent checks need an isolated subprocess entrypoint"
    spec = importlib.util.spec_from_file_location("agent_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_environment_is_allowlisted_and_does_not_modify_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "business-database")
    monkeypatch.setenv("LEARNING_TEST_MYSQL_URL", "business-test-database")
    monkeypatch.setenv("LEARNING_CONFIG_FILE", "business-config")
    monkeypatch.setenv("LEARNING_CHAT_API_KEY_ENV", "UNUSUAL_CREDENTIAL")
    monkeypatch.setenv("UNUSUAL_CREDENTIAL", "private-value")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-value")
    monkeypatch.setenv("RAG_TEST_REAL_MODELS", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    before = dict(os.environ)
    env = runner().isolated_environment(tmp_path)
    assert dict(os.environ) == before
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["DATABASE_URL"].startswith("sqlite+pysqlite:///")
    assert str(tmp_path.resolve()).replace("\\", "/") in env["DATABASE_URL"]
    for key in ("LEARNING_TEST_MYSQL_URL", "LEARNING_CONFIG_FILE", "UNUSUAL_CREDENTIAL",
                "DASHSCOPE_API_KEY", "RAG_TEST_REAL_MODELS", "PYTEST_ADDOPTS"):
        assert key not in env


def test_baseline_and_agent_selection_are_explicit_and_complete():
    module = runner()
    baseline = module.select_tests("baseline")
    assert len(baseline) == 8 and all(path.exists() for path in baseline)
    agent = module.select_tests("agent")
    assert {p.resolve() for p in agent} == set((BACKEND / "knowpath_backend/test").glob("test_web_agent_*.py"))
    compatible = module.select_tests("compatibility")
    assert set(baseline) <= set(compatible)
    assert len(compatible) == len(set(compatible))
    assert BACKEND / "knowpath_backend/test/test_function_call_cancel.py" in compatible
    for filename in ('test_simple_agent.py', 'test_react_agent.py',
                     'test_reflection_agent.py', 'test_playbook.py'):
        assert BACKEND / 'knowpath_backend/test' / filename in compatible


@pytest.mark.parametrize("url", ["sqlite:///daily.db", "mysql+pymysql://localhost/business",
                                  "mysql+pymysql://localhost/mysql", "mysql+pymysql://localhost/"])
def test_explicit_mysql_optin_rejects_non_test_database(tmp_path, url):
    with pytest.raises(ValueError, match="isolated.*MySQL"):
        runner().isolated_environment(tmp_path, mysql_url=url)


def test_explicit_mysql_test_url_is_only_added_by_argument(tmp_path):
    url = "mysql+pymysql://localhost/knowpath_agent_test"
    env = runner().isolated_environment(tmp_path, mysql_url=url)
    assert env["LEARNING_TEST_MYSQL_URL"] == url
    assert env["DATABASE_URL"].startswith("sqlite+")
    assert ["127.0.0.1", 3306] in json.loads(env["KNOWPATH_AGENT_ALLOWED_ADDRESSES"])


def test_full_suite_includes_nested_tests(tmp_path):
    nested = tmp_path / 'nested'
    nested.mkdir()
    test = nested / 'test_nested.py'
    test.write_text('def test_nested(): pass')
    module = runner()
    module.TESTS = tmp_path
    assert test in module.select_tests('full')


def test_network_guard_rejects_unconfigured_connections():
    from knowpath_backend.test import web_agent_guard
    with pytest.raises(RuntimeError, match="offline Agent checks"):
        web_agent_guard.check_address(("example.com", 443), allowed=())
    web_agent_guard.check_address(("localhost", 3307), allowed=(("localhost", 3307),))


def test_offline_guard_allows_asyncio_internal_socketpair():
    import asyncio

    async def local_work():
        return 42

    assert asyncio.run(local_work()) == 42
