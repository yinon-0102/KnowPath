"""Executable acceptance gates must fail closed, including collection drift."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[2]


def acceptance():
    spec = importlib.util.spec_from_file_location("acceptance_profiles_runner", BACKEND / "scripts/accept_learning_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_configuration_needs_no_model_keys_and_leaves_environment_unchanged(monkeypatch):
    module = acceptance()
    instance = module.Acceptance()
    monkeypatch.setattr(instance, "container", lambda name, image, port, env: str(port + 10000))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "business-database-must-not-change")
    before = dict(os.environ)
    assert hasattr(instance, "configure_storage"), "storage provisioning must be independent of model keys"
    env = instance.configure_storage()
    assert dict(os.environ) == before
    assert "127.0.0.1:13306/knowpath_acceptance" in env["LEARNING_TEST_MYSQL_URL"]
    assert env["DATABASE_URL"] == env["LEARNING_TEST_MYSQL_URL"]
    assert "127.0.0.1:13306/mysql" in env["LEARNING_TEST_MYSQL_ADMIN_URL"]
    assert env["RAG_TEST_QDRANT_URL"] == env["LEARNING_TEST_QDRANT_URL"]
    assert env["LEARNING_TEST_NEO4J_URI"].endswith(":17687")


def run_gate(tmp_path, source, *, required=None, extra=(), profile="store", matrix_data=None):
    test = tmp_path / "test_probe.py"
    test.write_text(source, encoding="utf-8")
    node = "test_probe.py::test_probe"
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(matrix_data if matrix_data is not None else
        {"store": required or [node], "model": [], "not_applicable": {}}), encoding="utf-8")
    report = tmp_path / "report.json"
    env = dict(os.environ, PYTHONPATH=str(BACKEND), PYTHON_DOTENV_DISABLED="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run([sys.executable, "-m", "scripts.acceptance_gate", "--profile", profile,
        "--matrix", str(matrix), "--report", str(report), "--", "--rootdir", str(tmp_path), "-q", "test_probe.py", *extra],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert report.exists(), result.stdout + result.stderr
    return result, json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize("source,extra,reason", [
    ("import pytest\ndef test_probe(): pytest.skip('missing dedicated service')\n", (), "unexpected_skip"),
    ("def test_probe(): pass\n", ("-k", "absent"), "selection_mismatch"),
    ("def test_probe(): pass\n", ("--collect-only",), "not_executed"),
    ("def test_probe(): assert False\n", (), "failed"),
    ("import pytest\n@pytest.mark.xfail\ndef test_probe(): assert False\n", (), "unexpected_skip"),
])
def test_gate_rejects_unexecuted_or_unsuccessful_required_cases(tmp_path, source, extra, reason):
    result, report = run_gate(tmp_path, source, extra=extra)
    assert result.returncode != 0
    assert reason in report["gate_errors"]


def test_gate_rejects_removed_required_case(tmp_path):
    result, report = run_gate(tmp_path, "def test_probe(): pass\n", required=["test_probe.py::test_removed"])
    assert result.returncode != 0
    assert "matrix_drift" in report["gate_errors"]


def test_gate_records_expected_execution_and_outcomes(tmp_path):
    result, report = run_gate(tmp_path, "def test_probe(): pass\n")
    assert result.returncode == 0
    assert report["expected"] == report["executed"] == ["test_probe.py::test_probe"]
    assert report["outcomes"]["test_probe.py::test_probe"] == "passed"
    assert report["gate_errors"] == []


def test_profile_environment_disables_business_endpoints_and_model_optin(monkeypatch):
    path = BACKEND / "scripts/run_acceptance.py"
    assert path.exists(), "three explicit profile entrypoints must be available"
    spec = importlib.util.spec_from_file_location("profile_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DATABASE_URL", "business")
    monkeypatch.setenv("LEARNING_TEST_MYSQL_URL", "business-too")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-value")
    monkeypatch.setenv("CUSTOM_SECRET", "another-private-value")
    monkeypatch.setenv("LEARNING_CHAT_API_KEY_ENV", "CUSTOM_SECRET")
    monkeypatch.setenv("RAG_TEST_REAL_MODELS", "1")
    env = module.profile_environment()
    assert env["DATABASE_URL"] == "sqlite+pysqlite:///:memory:"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["LEARNING_RETRIEVAL_BACKEND"] == "keyword"
    assert "LEARNING_LOCAL_TOKEN" not in env
    for key in ("LEARNING_TEST_MYSQL_URL", "DASHSCOPE_API_KEY", "CUSTOM_SECRET", "RAG_TEST_REAL_MODELS"):
        assert key not in env
    assert os.environ["DATABASE_URL"] == "business"


def test_store_profile_keeps_test_factory_defaults_while_setting_real_urls(monkeypatch):
    from scripts.run_acceptance import storage_test_environment
    instance = acceptance().Acceptance()
    monkeypatch.setattr(instance, "container", lambda name, image, port, env: str(port + 10000))
    env = storage_test_environment(instance)
    assert env["LEARNING_RETRIEVAL_BACKEND"] == "keyword"
    assert env["LEARNING_PERSISTENCE"] == "memory"
    assert "LEARNING_LOCAL_TOKEN" not in env
    assert "127.0.0.1:13306" in env["LEARNING_TEST_MYSQL_URL"]


def test_model_profile_preserves_explicit_frozen_settings_and_ignores_business_urls(tmp_path, monkeypatch):
    from scripts import run_acceptance
    assert hasattr(run_acceptance, "model_environment"), "explicit frozen model settings must reach subprocess"
    model_file = tmp_path / "model.env"
    model_file.write_text("DASHSCOPE_API_KEY=synthetic-test-key\nRAG_MODEL_OUTPUT_TOKENS=4000\n"
        "RAG_RERANK_API_KEY_ENV=RERANK_SECRET\nRERANK_SECRET=synthetic-rerank-key\n"
        "DATABASE_URL=business-must-not-be-loaded\nQDRANT_URL=business-must-not-be-loaded\n", encoding="utf-8")
    monkeypatch.setenv("RAG_RESPONSE_FORMAT", "json_schema")
    monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", "24000")
    monkeypatch.setenv("LEARNING_CONTEXT_BUDGET_TOKENS", "32768")
    monkeypatch.setenv("RAG_MAX_DRAFT_BYTES", "4000")
    monkeypatch.setenv("RAG_REVISION_GENERATION_SECONDS", "10")
    monkeypatch.setenv("RAG_REVISION_VERIFICATION_SECONDS", "25")
    monkeypatch.setenv("DATABASE_URL", "business-unchanged")
    before = dict(os.environ)
    env = run_acceptance.model_environment(acceptance().Acceptance(), model_file)
    assert dict(os.environ) == before
    for name in ("RAG_RESPONSE_FORMAT", "RAG_MODEL_INPUT_TOKENS", "LEARNING_CONTEXT_BUDGET_TOKENS",
                 "RAG_MAX_DRAFT_BYTES", "RAG_REVISION_GENERATION_SECONDS", "RAG_REVISION_VERIFICATION_SECONDS"):
        assert env[name] == before[name]
    assert env["RAG_MODEL_OUTPUT_TOKENS"] == "4000"
    assert env["RERANK_SECRET"] == "synthetic-rerank-key"
    assert env["DATABASE_URL"] == "sqlite+pysqlite:///:memory:"
    assert "QDRANT_URL" not in env


def test_profile_collection_includes_frozen_cases_outside_filename_globs():
    from scripts import run_acceptance
    assert hasattr(run_acceptance, "test_arguments"), "collect all frozen cases including non-RAG filenames"
    targets = run_acceptance.test_arguments()
    assert "knowpath_backend/test/test_project_root.py::test_storage_dirs" in targets
    matrix = json.loads((BACKEND / "scripts/acceptance_matrix.json").read_text(encoding="utf-8-sig"))
    for group in ("offline", "store", "model", "not_applicable"):
        for node in matrix[group]:
            assert node in targets or node.split("::")[0] in targets


def test_gate_adds_new_offline_cases_without_freezing_old_count(tmp_path):
    matrix = {"offline": ["test_probe.py::test_probe"], "store": [], "model": [], "not_applicable": {}}
    result, report = run_gate(tmp_path, "def test_probe(): pass\ndef test_added(): pass\n", profile="offline", matrix_data=matrix)
    assert result.returncode == 0
    assert len(report["expected"]) == len(report["executed"]) == 2


def test_gate_discovers_new_explicit_external_cases(tmp_path):
    source = "import pytest\ndef test_probe(): pass\n@pytest.mark.acceptance_store\ndef test_added(): pass\n"
    result, report = run_gate(tmp_path, source)
    assert result.returncode == 0
    assert len(report["executed"]) == 2


def test_gate_excludes_only_explicit_nonapplicable_case_and_records_reason(tmp_path):
    source = "def test_probe(): pass\ndef test_wrong_backend(): raise AssertionError('must not run')\n"
    matrix = {"store": ["test_probe.py::test_probe"], "model": [],
              "not_applicable": {"test_probe.py::test_wrong_backend": "row locks require MySQL"}}
    result, report = run_gate(tmp_path, source, matrix_data=matrix)
    assert result.returncode == 0
    assert report["not_applicable"] == matrix["not_applicable"]
    assert report["executed"] == ["test_probe.py::test_probe"]
