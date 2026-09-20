import json
from copy import deepcopy
from dataclasses import replace
import time

import httpx
import pytest

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.reranking import DashScopeReranker
from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.learning.rag.verification import VerificationError


MESSAGES = [{"role": "system", "content": "Output JSON."}, {"role": "user", "content": "问题"}]
CHUNKS = [{"chunk_id": "a", "retrieval_text": "课本 > 定义\n甲", "source_text": "甲"},
          {"chunk_id": "b", "retrieval_text": "课本 > 条件\n乙", "source_text": "乙"}]


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("RAG_TEST_SECRET", "private-secret-never-log")
    for name in ("RAG_RERANK_BASE_URL", "RAG_RERANK_MODEL", "RAG_RERANK_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)
    return LearningSettings(chat_api_key_env="RAG_TEST_SECRET", embedding_api_key_env="RAG_TEST_SECRET")


def model_response(content='{"ok":true}', finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}


def rerank_response(rows=None):
    return {"output": {"results": rows if rows is not None else [
        {"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]},
        "usage": {"total_tokens": 32}}


def test_model_posts_one_compatible_json_request_with_deadline_and_usage(settings):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=model_response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = BudgetedJsonModel(settings, client=client, max_input_tokens=2000, max_output_tokens=100)
        assert model.generate_json(MESSAGES, deadline=time.monotonic() + 3) == {"ok": True}
        assert not client.is_closed
    assert len(calls) == 1
    request = calls[0]
    body = json.loads(request.content)
    assert str(request.url).endswith("/compatible-mode/v1/chat/completions")
    assert body["messages"] == MESSAGES
    assert body["model"] == settings.chat_model and body["enable_thinking"] is False
    assert body["max_tokens"] == 100 and body["response_format"] == {"type": "json_object"}
    assert 0 < request.extensions["timeout"]["read"] <= 3
    assert model.last_usage["prompt_tokens"] == 20 and model.last_usage["completion_tokens"] == 5
    assert "messages" not in model.last_usage and "private-secret" not in repr(model.last_usage)


def test_other_compatible_provider_does_not_receive_dashscope_fields(settings):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json=model_response()))[1])) as client:
        BudgetedJsonModel(replace(settings, chat_provider="openai"), client=client).generate_json(MESSAGES, deadline=time.monotonic()+5)
    assert "enable_thinking" not in json.loads(calls[0].content)


@pytest.mark.parametrize("kind", ["input", "output_reserve", "deadline"])
def test_model_rejects_budget_or_deadline_before_network(settings, kind):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        model = BudgetedJsonModel(replace(settings, context_budget_tokens=300), client=client,
                                  max_input_tokens=10 if kind == "input" else 2000,
                                  max_output_tokens=290 if kind == "output_reserve" else 20)
        with pytest.raises(VerificationError) as error:
            model.generate_json(MESSAGES, deadline=time.monotonic() + (-1 if kind == "deadline" else 5))
    assert error.value.code == ("RAG_DEADLINE_EXCEEDED" if kind == "deadline" else "MODEL_TOKEN_BUDGET_EXCEEDED")
    assert calls == []


@pytest.mark.parametrize("content,finish", [('[]', 'stop'), ('{"ok":true}', 'length'),
    ('```json\n{}\n```', 'stop'), ('{"x":NaN}', 'stop'), ('{"x":1,"x":2}', 'stop'),
    ('{"nested":[1e999]}', 'stop')])
def test_model_rejects_non_object_truncation_or_non_strict_json(settings, content, finish):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json=model_response(content, finish)))[1])) as client:
        model = BudgetedJsonModel(settings, client=client)
        with pytest.raises(VerificationError, match="MODEL_INVALID_RESPONSE"):
            model.generate_json(MESSAGES, deadline=time.monotonic()+5)
    assert len(calls) == 1


@pytest.mark.parametrize("service", ["model", "rerank"])
def test_http_failure_is_secret_safe_and_never_retried(settings, service):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="private-secret-never-log prompt from provider")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = BudgetedJsonModel(settings, client=client) if service == "model" else DashScopeReranker(settings, client=client)
        with pytest.raises(VerificationError if service == "model" else RetrievalError) as error:
            if service == "model":
                adapter.generate_json(MESSAGES, deadline=time.monotonic()+5)
            else:
                adapter.rerank("问题", CHUNKS, deadline=time.monotonic()+5)
    assert len(calls) == 1
    assert str(error.value) == ("MODEL_UNAVAILABLE" if service == "model" else "RERANK_UNAVAILABLE")
    assert error.value.__cause__ is None


def test_reranker_uses_separate_endpoint_titles_exact_indices_and_copies(settings):
    calls, original = [], deepcopy(CHUNKS)
    with httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json=rerank_response()))[1])) as client:
        adapter = DashScopeReranker(settings, client=client)
        rows = adapter.rerank("问题", CHUNKS, deadline=time.monotonic()+4)
        assert not client.is_closed
    assert len(calls) == 1
    assert str(calls[0].url) == "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    body = json.loads(calls[0].content)
    assert body == {"model": "gte-rerank-v2", "input": {"query": "问题", "documents": [c["retrieval_text"] for c in CHUNKS]},
                    "parameters": {"top_n": 2, "return_documents": False}}
    assert [c["chunk_id"] for c in rows] == ["b", "a"]
    assert rows[0]["rerank_score"] == 0.9 and rows[0] is not CHUNKS[1]
    assert CHUNKS == original and adapter.last_usage["total_tokens"] == 32
    assert 0 < calls[0].extensions["timeout"]["read"] <= 4


@pytest.mark.parametrize("rows", [[], [{"index": 0, "relevance_score": 1}],
    [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": .5}],
    [{"index": 0, "relevance_score": 1}, {"index": 2, "relevance_score": .5}],
    [{"index": 0, "relevance_score": 1}, {"index": True, "relevance_score": .5}],
    [{"index": 0, "relevance_score": 1}, {"index": 1, "relevance_score": "NaN"}]])
def test_reranker_rejects_missing_duplicate_invalid_or_nonfinite_ranking(settings, rows):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rerank_response(rows)))) as client:
        with pytest.raises(RetrievalError, match="RERANK_INVALID_RESPONSE"):
            DashScopeReranker(settings, client=client).rerank("问题", CHUNKS, deadline=time.monotonic()+5)


@pytest.mark.parametrize("kind", ["candidates", "tokens", "deadline"])
def test_rerank_resource_limits_fail_before_network(settings, kind):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        adapter = DashScopeReranker(settings, client=client, max_candidates=1 if kind == "candidates" else 40,
                                    max_input_tokens=1 if kind == "tokens" else 12000)
        with pytest.raises(RetrievalError):
            adapter.rerank("问题", CHUNKS, deadline=time.monotonic()+(-1 if kind == "deadline" else 5))
    assert calls == []


@pytest.mark.parametrize("service", ["model", "rerank"])
def test_late_success_is_rejected_after_deadline(settings, monkeypatch, service):
    clock = {"now": 10.0}
    monkeypatch.setattr("knowpath_backend.learning.rag.model_services.time.monotonic", lambda: clock["now"])
    calls = []
    def handler(request):
        calls.append(request)
        clock["now"] = 20.0
        return httpx.Response(200, json=model_response() if service == "model" else rerank_response())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = BudgetedJsonModel(settings, client=client) if service == "model" else DashScopeReranker(settings, client=client)
        with pytest.raises(VerificationError if service == "model" else RetrievalError, match="RAG_DEADLINE_EXCEEDED"):
            if service == "model":
                adapter.generate_json(MESSAGES, deadline=15)
            else:
                adapter.rerank("问题", CHUNKS, deadline=15)
        assert adapter.last_usage is None
    assert len(calls) == 1


def test_rerank_env_and_explicit_configuration_are_independent(settings, monkeypatch):
    monkeypatch.setenv("RAG_RERANK_BASE_URL", "https://rerank.example/rank")
    monkeypatch.setenv("RAG_RERANK_MODEL", "experiment-model")
    monkeypatch.setenv("RAG_RERANK_API_KEY_ENV", "ALTERNATE_RERANK_KEY")
    monkeypatch.setenv("ALTERNATE_RERANK_KEY", "alternate-key")
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json=rerank_response()))[1])) as client:
        DashScopeReranker(settings, client=client).rerank("问题", CHUNKS, deadline=time.monotonic()+5)
        DashScopeReranker(settings, client=client, endpoint="https://explicit.example/rank", model="explicit", key_env="RAG_TEST_SECRET").rerank("问题", CHUNKS, deadline=time.monotonic()+5)
    assert str(calls[0].url) == "https://rerank.example/rank"
    assert json.loads(calls[0].content)["model"] == "experiment-model"
    assert calls[0].headers["Authorization"] == "Bearer alternate-key"
    assert str(calls[1].url) == "https://explicit.example/rank"
    assert json.loads(calls[1].content)["model"] == "explicit"
    assert calls[1].headers["Authorization"] == "Bearer private-secret-never-log"


@pytest.mark.parametrize("service", ["model", "rerank"])
def test_missing_credentials_make_no_network_call(settings, monkeypatch, service):
    monkeypatch.delenv("RAG_TEST_SECRET")
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        with pytest.raises(VerificationError if service == "model" else RetrievalError):
            if service == "model":
                BudgetedJsonModel(settings, client=client).generate_json(MESSAGES, deadline=time.monotonic()+5)
            else:
                DashScopeReranker(settings, client=client).rerank("问题", CHUNKS, deadline=time.monotonic()+5)
    assert calls == []


def test_unhashable_message_role_is_a_stable_input_error(settings):
    with pytest.raises(VerificationError, match="MODEL_INPUT_INVALID"):
        BudgetedJsonModel(settings).generate_json([{"role": [], "content": "private prompt"}], deadline=time.monotonic()+5)
