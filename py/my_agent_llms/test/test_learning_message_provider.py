"""Provider boundary tests use HTTP doubles and never contact a model."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.message_generation import DashScopeAnswerGenerator, MessageGenerationError, validate_answer
from my_agent_llms.test.test_learning_messages import FixedAnswer


def snapshot():
    return {"message": "Explain", "history": [], "sources": [{"chunk_id": "chunk-1", "material_id": "m", "material_version_id": "v", "text": "Reusable behavior"}]}


def test_provider_request_and_source_validation(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    def handle(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        assert len(body["messages"]) == 2
        assert json.loads(body["messages"][1]["content"]) == snapshot()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"text": "Explanation", "citation_ids": ["chunk-1"]})}}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        raw = DashScopeAnswerGenerator(client=client).generate(snapshot())
    text, refs = validate_answer(raw, snapshot()["sources"])
    assert text == "Explanation" and "text" not in refs[0]


@pytest.mark.parametrize("response", [httpx.Response(401, text="test-secret"), httpx.Response(302, headers={"location": "https://example.invalid/steal"})])
def test_provider_http_errors_redact_content(monkeypatch, response):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(MessageGenerationError) as exc:
            DashScopeAnswerGenerator(client=client).generate(snapshot())
    assert str(exc.value) == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, {"choices": [{"message": {"content": "invalid JSON"}}]}])
def test_provider_rejects_malformed_and_truncated_output(monkeypatch, body):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(MessageGenerationError) as exc:
            DashScopeAnswerGenerator(client=client).generate(snapshot())
    assert exc.value.code == "MESSAGE_VALIDATION_FAILED"


def test_provider_missing_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(MessageGenerationError, match="MODEL_UNAVAILABLE"):
        DashScopeAnswerGenerator().generate(snapshot())


@pytest.mark.parametrize("raw", [{"text": " ", "citation_ids": ["chunk-1"]}, {"text": "ok", "citation_ids": []}, {"text": "ok", "citation_ids": ["chunk-1", "chunk-1"]}, {"text": "ok", "citation_ids": [["chunk-1"]]}, {"text": "ok", "citation_ids": ["foreign"]}, {"text": "ok", "citation_ids": ["chunk-1"], "hidden": "x"}])
def test_answer_schema_rejects_untrusted_output(raw):
    with pytest.raises(MessageGenerationError):
        validate_answer(raw, snapshot()["sources"])


@pytest.mark.parametrize("payload", [{"message": ""}, {"message": "   "}, {"message": "x" * 8001}, {"message": 123}, {"message": "ok", "stream": False}, {"message": "ok", "stream": "true"}, {"message": "ok", "session_id": ""}, {"message": "ok", "unexpected": 1}])
def test_http_message_contract_rejects_invalid_request(payload):
    with TestClient(create_app(answer_generator=FixedAnswer())) as client:
        response = client.post("/api/v1/learning-spaces/missing/messages", json=payload, headers={"Idempotency-Key": "invalid"})
    assert response.status_code == 422


def test_http_message_requires_idempotency():
    with TestClient(create_app(answer_generator=FixedAnswer())) as client:
        response = client.post("/api/v1/learning-spaces/missing/messages", json={"message": "Explain"})
    assert response.status_code == 400
