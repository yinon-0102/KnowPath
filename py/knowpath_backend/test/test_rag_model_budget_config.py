"""Explicit model budgets shared by online execution and frozen evaluation.

These regressions intentionally precede implementation. No real provider or
database is used, and candidate/context/call budgets remain the existing defaults.
"""
import time
from types import SimpleNamespace

import httpx
import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag import runtime
from knowpath_backend.learning.rag.contracts import RetrievalBudget
from knowpath_backend.rag_eval import cli


@pytest.fixture(autouse=True)
def explicit_budget_environment(monkeypatch):
    for name in ("RAG_MODEL_INPUT_TOKENS", "RAG_MODEL_OUTPUT_TOKENS", "RAG_MAX_REQUEST_COST", "RAG_PRICING_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_default_model_budget_remains_twelve_thousand_plus_two_thousand():
    assert runtime.model_budget_configuration(LearningSettings()) == {
        "model_input_tokens": 12000, "model_output_tokens": 2000}


def test_explicit_model_budget_accepts_exact_context_capacity(monkeypatch):
    monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", "24000")
    monkeypatch.setenv("RAG_MODEL_OUTPUT_TOKENS", "4000")
    assert runtime.model_budget_configuration(LearningSettings(context_budget_tokens=28000)) == {
        "model_input_tokens": 24000, "model_output_tokens": 4000}


@pytest.mark.parametrize("name", ["RAG_MODEL_INPUT_TOKENS", "RAG_MODEL_OUTPUT_TOKENS"])
@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "1.5", "1e3", " 12", "12 ", "true", "nan", "inf", "１２"])
def test_model_budget_rejects_nonpositive_or_noninteger_environment_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="RAG_MODEL_BUDGET_INVALID"):
        runtime.model_budget_configuration(LearningSettings())


@pytest.mark.parametrize("input_tokens,output_tokens,context_tokens", [
    ("24000", "4000", 27999), ("15000", "2000", 16000), (None, None, 13999),
])
def test_model_budget_rejects_combined_context_overflow(monkeypatch, input_tokens, output_tokens, context_tokens):
    if input_tokens is not None:
        monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", input_tokens)
    if output_tokens is not None:
        monkeypatch.setenv("RAG_MODEL_OUTPUT_TOKENS", output_tokens)
    with pytest.raises(ValueError, match="RAG_MODEL_BUDGET_INVALID"):
        runtime.model_budget_configuration(LearningSettings(context_budget_tokens=context_tokens))


def _instance(settings):
    instance = runtime.ConfiguredPipeline.__new__(runtime.ConfiguredPipeline)
    instance.settings, instance.mode = settings, "a"
    instance.repo = instance.materials = instance.spaces = object()
    return instance


@pytest.mark.parametrize("value", ["bad-config-secret", "0", "999999999"])
def test_invalid_model_budget_stops_before_provider_construction(monkeypatch, value):
    monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", value)
    touched = []
    def provider(*args, **kwargs):
        touched.append(True)
        pytest.fail("invalid budget reached provider construction")
    monkeypatch.setattr(runtime, "embedding_model", provider)
    with pytest.raises(ValueError, match="RAG_MODEL_BUDGET_INVALID") as error:
        _instance(LearningSettings())._answer("query", deadline=time.monotonic() + 5)
    assert touched == []
    assert value not in str(error.value)


@pytest.mark.parametrize("configured", [False, True])
def test_generator_checker_and_eval_use_identical_model_budgets_without_changing_retrieval(monkeypatch, configured):
    settings = LearningSettings(context_budget_tokens=32000)
    if configured:
        monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", "24000")
        monkeypatch.setenv("RAG_MODEL_OUTPUT_TOKENS", "4000")
    expected = (24000, 4000) if configured else (12000, 2000)
    captured = {}
    def reject_http(request):
        pytest.fail("budget construction must not call a real service")
    monkeypatch.setattr(runtime, "DeadlineTransport", lambda *args: httpx.MockTransport(reject_http))
    monkeypatch.setattr(runtime, "embedding_model", lambda *args, **kwargs:
        SimpleNamespace(model_version=settings.embedding_model, dimension=settings.embedding_dimension))
    monkeypatch.setattr(runtime, "QdrantClient", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runtime, "QdrantContentIndex", lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, "create_plugin", lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, "DashScopeReranker", lambda *args, **kwargs: object())
    class Pipeline:
        def __init__(self, repository, materials, spaces, plugin, reranker, verifier, *, budget, **kwargs):
            captured.update(generator=verifier.generator, checker=verifier.checker, budget=budget)
        def answer(self, *args, **kwargs):
            return {"trace": {}}
    monkeypatch.setattr(runtime, "RagPipeline", Pipeline)
    _instance(settings)._answer("query", deadline=time.monotonic() + 5)
    for model in (captured["generator"], captured["checker"]):
        assert (model.max_input_tokens, model.max_output_tokens) == expected
    assert captured["budget"] == RetrievalBudget()
    frozen = cli.runtime_configuration(settings)["budgets"]
    assert (frozen["model_input_tokens"], frozen["model_output_tokens"]) == expected
    for key, value in RetrievalBudget().model_dump().items():
        assert frozen[key] == value
    assert frozen["rerank_input_tokens"] == 90000 and frozen["transport_retries"] == 0


def test_eval_reads_the_shared_model_budget_function(monkeypatch):
    calls = []
    def shared(settings):
        calls.append(settings)
        return {"model_input_tokens": 13000, "model_output_tokens": 1000}
    monkeypatch.setattr(runtime, "model_budget_configuration", shared, raising=False)
    settings = LearningSettings()
    actual = cli.runtime_configuration(settings)
    assert calls == [settings]
    assert actual["budgets"]["model_input_tokens"] == 13000
    assert actual["budgets"]["model_output_tokens"] == 1000


def test_changed_model_budget_rejects_previous_freeze_before_database_or_provider(monkeypatch):
    from knowpath_backend.learning.persistence import db
    settings = LearningSettings(context_budget_tokens=32000)
    monkeypatch.setattr(LearningSettings, "from_env", classmethod(lambda cls: settings))
    frozen = {"config": cli.runtime_configuration(settings)}
    monkeypatch.setenv("RAG_MODEL_INPUT_TOKENS", "24000")
    monkeypatch.setenv("RAG_MODEL_OUTPUT_TOKENS", "4000")
    def reject_database(*args, **kwargs):
        pytest.fail("mismatched frozen budget reached database construction")
    monkeypatch.setattr(db, "create_db_engine", reject_database)
    with pytest.raises(ValueError, match="frozen models/prompts/budgets/environment"):
        cli.configured_runtime(frozen)
