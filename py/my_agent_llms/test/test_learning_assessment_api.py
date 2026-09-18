from my_agent_llms.test.material_upload_helpers import ParsedUploadClient
import pytest

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.question_generation import QuestionGenerationError
from my_agent_llms.test.test_learning_state_persistence import FixedQuestions


@pytest.fixture
def assessment_api():
    client = ParsedUploadClient(create_app(question_generator=FixedQuestions()))
    material = client.post("/api/v1/materials", files={"file": ("notes.md", b"# Functions\n\nReusable behavior.", "text/markdown")}, headers={"Idempotency-Key": "material"}).json()
    space = client.post("/api/v1/learning-spaces", json={"name": "Python", "material_ids": [material["material"]["id"]]}, headers={"Idempotency-Key": "space"}).json()
    yield client, space["id"]
    client.close()


def create(client, space_id, key="assessment"):
    return client.post(f"/api/v1/learning-spaces/{space_id}/assessments", json={"question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}, headers={"Idempotency-Key": key})


@pytest.mark.parametrize("payload", [{"question_count": 1}, {"question_count": True}, {"unknown": 1}, {"question_types": ["essay"]}, {"difficulty_mix": {"easy": 0.1, "medium": 0.1, "hard": 0.1}}])
def test_create_rejects_invalid_request(assessment_api, payload):
    client, space_id = assessment_api
    response = client.post(f"/api/v1/learning-spaces/{space_id}/assessments", json=payload, headers={"Idempotency-Key": "invalid"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_failed_model_preserves_failed_run_and_replay(assessment_api):
    client, space_id = assessment_api
    class Unavailable:
        def generate(self, topics, payload):
            raise QuestionGenerationError("MODEL_UNAVAILABLE")
    client.app.state.learning_state.assessment_service.generator = Unavailable()
    response = create(client, space_id)
    assert response.status_code == 202
    assert create(client, space_id).json() == response.json()
    assessment = client.get(f"/api/v1/assessments/{response.json()['assessment_id']}").json()
    assert assessment["status"] == "failed"
    assert assessment["questions"] == []
    run = client.app.state.learning_state.get_run(response.json()["run_id"])
    assert run["error"]["code"] == "MODEL_UNAVAILABLE"
    assert client.get(f"/api/v1/learning-spaces/{space_id}/evidence").json()["items"] == []


def test_answers_hidden_validation_finalize_and_evidence_pagination(assessment_api):
    client, space_id = assessment_api
    created = create(client, space_id).json()
    aid = created["assessment_id"]
    assessment = client.get(f"/api/v1/assessments/{aid}").json()
    assert len(assessment["questions"]) == 5
    assert not any(k in str(assessment) for k in ("answer_key", "rubric", "source_refs"))
    assert client.get(f"/api/v1/assessments/{aid}/result").status_code == 409
    answers = [{"question_id": q["id"], "answer": "A", "expected_answer_revision": 0} for q in assessment["questions"]]
    missing = client.post(f"/api/v1/assessments/{aid}/attempts", json={"answers": answers})
    assert missing.status_code == 400
    bad = client.post(f"/api/v1/assessments/{aid}/attempts", json={"answers": [dict(answers[0], answer="Z")]}, headers={"Idempotency-Key": "bad"})
    assert bad.status_code == 422
    submitted = client.post(f"/api/v1/assessments/{aid}/attempts", json={"answers": answers, "finalize": True}, headers={"Idempotency-Key": "submit"})
    assert submitted.status_code == 202
    assert client.post(f"/api/v1/assessments/{aid}/finalize", json={}).json() == submitted.json()
    path = f"/api/v1/learning-spaces/{space_id}/evidence"
    page = client.get(path, params={"limit": 2}).json()
    ids = [e["id"] for e in page["items"]]
    assert page["next_cursor"]
    while page["next_cursor"]:
        page = client.get(path, params={"limit": 2, "cursor": page["next_cursor"]}).json()
        ids.extend(e["id"] for e in page["items"])
    assert len(set(ids)) == len(ids) == 5
    assert client.get(path, params={"topic_id": "absent"}).json()["items"] == []
    assert client.get(path, params={"from": "2099-01-01T00:00:00Z"}).json()["items"] == []
    assert client.get(path, params={"from": "2099-01-01", "to": "2020-01-01"}).status_code == 422
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert client.get(path, params={"cursor": "invalid"}).status_code == 422
