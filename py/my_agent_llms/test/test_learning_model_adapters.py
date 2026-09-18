import json
import httpx
import pytest
from my_agent_llms.learning.config import LearningSettings
from my_agent_llms.learning.model_adapters import chat_model, ModelError


def test_chat_adapter_structured_tools_and_stream_use_same_port(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    def handle(request):
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
        if body.get("tools"):
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = chat_model(LearningSettings(), client=client)
        assert model.generate_json([]) == {"ok": True}
        assert model.generate([], tools=[{"type": "function", "function": {"name": "lookup"}}])["tool_calls"][0]["id"] == "call"
        assert list(model.stream([])) == ["Hello"]
        assert not client.is_closed


def test_unsupported_provider_and_model_rejected_before_network():
    for settings in [LearningSettings(chat_provider="unknown"), LearningSettings(chat_model="embedding-only")]:
        with pytest.raises(ModelError) as error:
            chat_model(settings)
        assert error.value.code == "UNSUPPORTED_MODEL"


def test_model_retries_transient_transport_failure_without_leaking(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    calls = []
    def handle(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, text="private upstream data")
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert chat_model(LearningSettings(), client=client).generate([])["content"] == "recovered"
    assert len(calls) == 3


@pytest.mark.parametrize("status,code", [(429,"RATE_LIMITED"), (503,"MODEL_UNAVAILABLE")])
def test_exhausted_provider_retries_keep_error_classification(monkeypatch,status,code):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        with pytest.raises(ModelError) as error:
            chat_model(LearningSettings(),client=client).generate_json([])
    assert error.value.code == code

@pytest.mark.parametrize("choice", [None, [], "invalid", 7])
def test_invalid_choice_is_safe_validation_error(monkeypatch,choice):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={"choices":[choice]}))) as client:
        with pytest.raises(ModelError) as error:
            chat_model(LearningSettings(),client=client).generate_json([])
    assert error.value.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("route,body", [("assessments", {"kind":"diagnostic","question_count":5}), ("messages", {"message":"Explain this"})])
def test_http_unsupported_model_rejected_before_task_creation(route,body):
    from fastapi.testclient import TestClient
    from my_agent_llms.learning.api import create_app
    app = create_app(settings=LearningSettings(chat_provider="unsupported"))
    with TestClient(app) as client:
        response = client.post(f"/api/v1/learning-spaces/space/{route}",json=body,headers={"Idempotency-Key":"unsupported"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_MODEL"


def test_unsupported_embedding_does_not_crash_api_startup(monkeypatch):
    from fastapi.testclient import TestClient
    from my_agent_llms.learning.api import create_app
    from my_agent_llms.learning.vector_retrieval import KeywordRetriever
    monkeypatch.setenv("LEARNING_RETRIEVAL_BACKEND", "qdrant")
    app = create_app(settings=LearningSettings(embedding_provider="unsupported"))
    with TestClient(app) as client:
        response = client.post("/api/v1/learning-spaces/space/messages", json={"message":"explain"}, headers={"Idempotency-Key":"unsupported-embedding"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_MODEL"
