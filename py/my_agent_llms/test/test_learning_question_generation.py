"""Question generation contract tests; all model traffic uses MockTransport."""
import copy
import json
from uuid import UUID
import httpx
import pytest
from my_agent_llms.learning.config import LearningSettings
from my_agent_llms.learning.question_generation import DashScopeQuestionGenerator, QuestionGenerationError, validate_questions

@pytest.fixture
def topics():
    return [{"id": "topic-1", "name": "Functions", "revision_id": "rev-1", "source_text": "Functions return values.", "source_refs": [{"material_id": "mat-1", "material_version_id": "mv-1", "chunk_id": "chunk-1", "page": None, "line_start": 1, "line_end": 2}]}]

@pytest.fixture
def payload():
    return {"kind": "diagnostic", "topic_ids": ["topic-1"], "question_count": 5, "question_types": ["single_choice", "short_answer"], "difficulty_mix": {"easy": .3, "medium": .5, "hard": .2}}

@pytest.fixture
def raw(topics):
    return [{"type": "single_choice", "prompt": f"What does function {i} return?", "topic_id": "topic-1", "options": [{"id": "A", "text": "A value"}, {"id": "B", "text": "Nothing"}], "answer_key": "A", "rubric": "Select A.", "source_refs": copy.deepcopy(topics[0]["source_refs"]), "difficulty": ["easy", "easy", "medium", "medium", "hard"][i], "is_application": False} for i in range(5)]

def test_server_owned_metadata_and_stable_family(raw, topics, payload):
    raw[0].update(id="forged", family_id="forged", topic_revision_id="wrong", rubric_version="wrong")
    first = validate_questions(raw, topics, payload)
    second = validate_questions(raw, topics, payload)
    UUID(first[0]["id"])
    assert first[0]["id"] != second[0]["id"]
    assert first[0]["family_id"] == second[0]["family_id"] != "forged"
    assert first[0]["topic_revision_id"] == "rev-1"
    assert first[0]["rubric_version"] == "objective-v1"
    assert raw[0]["id"] == "forged"
    raw[0]["prompt"] = " WHAT   does function 0 return? "
    raw[0]["options"].reverse()
    assert validate_questions(raw, topics, payload)[0]["family_id"] == first[0]["family_id"]
    raw[0]["prompt"] = "What arguments does function 0 receive?"
    assert validate_questions(raw, topics, payload)[0]["family_id"] != first[0]["family_id"]

def test_short_answer_needs_rubric_not_answer_key(raw, topics, payload):
    raw[0].update(type="short_answer", options=[], rubric="Explain return values.")
    del raw[0]["answer_key"]
    result = validate_questions(raw, topics, payload)
    assert result[0]["rubric_version"] == "short-answer-v1"
    assert result[0]["answer_key"] is None

@pytest.mark.parametrize("field,value", [("type", "essay"), ("topic_id", "foreign"), ("prompt", "  "), ("prompt", "x" * 8001), ("rubric", ""), ("answer_key", "C"), ("source_refs", []), ("source_refs", [{"chunk_id": "invented"}]), ("difficulty", "extreme"), ("is_application", "false"), ("options", [{"id": "A", "text": "one"}]), ("options", [{"id": "A", "text": "one"}, {"id": "A", "text": "two"}]), ("options", [{"id": "A", "text": " One "}, {"id": "B", "text": "one"}])])
def test_invalid_question_rejects_whole_set(raw, topics, payload, field, value):
    raw[2][field] = value
    with pytest.raises(QuestionGenerationError) as error:
        validate_questions(raw, topics, payload)
    assert error.value.code == "QUESTION_VALIDATION_FAILED"

def test_count_duplicates_type_and_quota(raw, topics, payload):
    variants = [raw[:-1], [*raw[:-1], copy.deepcopy(raw[0])]]
    bad_quota = copy.deepcopy(raw)
    bad_quota[0]["difficulty"] = "hard"
    variants.append(bad_quota)
    for variant in variants:
        with pytest.raises(QuestionGenerationError):
            validate_questions(variant, topics, payload)
    payload["question_types"] = ["short_answer"]
    with pytest.raises(QuestionGenerationError):
        validate_questions(raw, topics, payload)

def test_cross_topic_source_and_modified_location(raw, topics, payload):
    topics.append({**copy.deepcopy(topics[0]), "id": "other", "revision_id": "rev-2"})
    topics[1]["source_refs"][0]["chunk_id"] = "other-chunk"
    payload["topic_ids"].append("other")
    raw[0]["source_refs"] = copy.deepcopy(topics[1]["source_refs"])
    with pytest.raises(QuestionGenerationError):
        validate_questions(raw, topics, payload)
    raw[0]["source_refs"] = copy.deepcopy(topics[0]["source_refs"])
    raw[0]["source_refs"][0]["line_end"] = 999
    with pytest.raises(QuestionGenerationError):
        validate_questions(raw, topics, payload)

def test_missing_mix_allows_any_valid_difficulty(raw, topics, payload):
    del payload["difficulty_mix"]
    raw[0]["difficulty"] = "hard"
    assert len(validate_questions(raw, topics, payload)) == 5

def test_json_request_source_and_largest_remainder(monkeypatch, topics, payload, raw):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.delenv("LEARNING_CHAT_BASE_URL", raising=False)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"questions": raw})}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert DashScopeQuestionGenerator(client=client).generate(topics, payload) == raw
    assert str(requests[0].url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "qwen-plus"
    assert body["response_format"] == {"type": "json_object"}
    assert body["enable_thinking"] is False
    assert "untrusted" in body["messages"][0]["content"].lower()
    supplied = json.loads(body["messages"][1]["content"])
    assert supplied["topics"][0]["source_text"] == topics[0]["source_text"]
    assert supplied["difficulty_counts"] == {"easy": 2, "medium": 2, "hard": 1}

@pytest.mark.parametrize("response", [httpx.Response(503, text="private upstream body"), httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"choices": [{"message": {"content": "bad private JSON"}}]}), httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}), httpx.Response(200, json={"choices": [{"message": {"content": '{"questions":[]}'}, "finish_reason": "length"}]})])
def test_upstream_failure_safe(monkeypatch, topics, payload, response):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(QuestionGenerationError) as error:
            DashScopeQuestionGenerator(client=client).generate(topics, payload)
    assert error.value.code in {"MODEL_UNAVAILABLE", "QUESTION_VALIDATION_FAILED"}
    assert "private" not in str(error.value)
    assert "test-secret" not in str(error.value)

def test_timeout_safe(monkeypatch, topics, payload):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    def timeout(request):
        raise httpx.ReadTimeout("private timeout")
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(QuestionGenerationError) as error:
            DashScopeQuestionGenerator(client=client).generate(topics, payload)
    assert error.value.code == "MODEL_UNAVAILABLE"
    assert "private" not in str(error.value)

@pytest.mark.parametrize("provider,key", [("dashscope", ""), ("unsupported", "secret")])
def test_invalid_configuration_never_calls_network(monkeypatch, topics, payload, provider, key):
    monkeypatch.setenv("DASHSCOPE_API_KEY", key)
    def forbidden(request):
        pytest.fail("Invalid configuration must not call a model")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(QuestionGenerationError) as error:
            DashScopeQuestionGenerator(LearningSettings(chat_provider=provider), client=client).generate(topics, payload)
    assert error.value.code == "MODEL_UNAVAILABLE"

@pytest.mark.parametrize("patch", [
    {"question_count": 4}, {"question_count": True}, {"question_count": 11},
    {"question_types": []}, {"topic_ids": ["unknown"]},
    {"difficulty_mix": {"easy": 0.7}},
    {"difficulty_mix": {"easy": float("nan"), "medium": 1}},
    {"difficulty_mix": {"easy": True}},
    {"difficulty_mix": {"easy": -0.2, "medium": 1.2}},
])
def test_invalid_constraints_rejected(raw, topics, payload, patch):
    payload.update(patch)
    with pytest.raises(QuestionGenerationError) as error:
        validate_questions(raw, topics, payload)
    assert error.value.code == "QUESTION_VALIDATION_FAILED"


def test_source_less_topic_fails_before_model_call(monkeypatch, topics, payload):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    topics[0]["source_text"] = " "
    def forbidden(request):
        pytest.fail("Insufficient sources must not invoke model")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(QuestionGenerationError) as error:
            DashScopeQuestionGenerator(client=client).generate(topics, payload)
    assert error.value.code == "QUESTION_VALIDATION_FAILED"


def test_configured_endpoint_and_unselected_sources(monkeypatch, topics, payload, raw):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    monkeypatch.setenv("LEARNING_CHAT_BASE_URL", "https://model.example/compatible-mode/v1/")
    topics.append({**copy.deepcopy(topics[0]), "id": "unselected", "source_text": "excluded text"})
    def respond(request):
        assert str(request.url) == "https://model.example/compatible-mode/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "configured-model"
        assert "excluded text" not in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"questions": raw})}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert len(DashScopeQuestionGenerator(LearningSettings(chat_model="configured-model"), client=client).generate(topics, payload)) == 5
        assert not client.is_closed


def test_invalid_model_questions_never_become_generated_fallback(monkeypatch, topics, payload, raw):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    raw[0]["source_refs"][0]["chunk_id"] = "invented"
    response = {"choices": [{"message": {"content": json.dumps({"questions": raw})}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
        with pytest.raises(QuestionGenerationError) as error:
            DashScopeQuestionGenerator(client=client).generate(topics, payload)
    assert error.value.code == "QUESTION_VALIDATION_FAILED"
