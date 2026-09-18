"""Grade reviews replace evidence without creating a second observation."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from my_agent_llms.learning.errors import DomainConflict, DomainNotFound
from my_agent_llms.test.test_learning_state_persistence import workspace, create, answer


def completed(factory, space_id):
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    return state, assessment


def test_review_replaces_evidence_and_replays_after_restart(workspace):
    factory, space_id, _, _ = workspace
    state, assessment = completed(factory, space_id)
    question_id = assessment["questions"][0]["id"]
    before = state.get_state(space_id)["items"][0]
    old_result = state.assessment_result(assessment["id"])
    old_question = next(q for q in old_result["question_results"] if q["question_id"] == question_id)
    body = {"question_id": question_id, "reason": "请按原标准核查"}
    response = state.grade_review(assessment["id"], body, idempotency_key="review")
    assert factory().grade_review(assessment["id"], body, idempotency_key="review") == response
    assert factory().get_run(response["run_id"])["status"] == "succeeded"
    after = factory().get_state(space_id)["items"][0]
    assert after["mastery_score"] == before["mastery_score"]
    assert after["independent_evidence_count"] == before["independent_evidence_count"]
    assert after["evidence_count"] == before["evidence_count"]
    assert after["next_review_at"] == before["next_review_at"]
    records = factory().assessment_service.repository.records("evidence", space_id=space_id)
    old = next(e for e in records if e["id"] == old_question["evidence_id"])
    new = next(e for e in records if e.get("replaces_evidence_id") == old["id"])
    assert not old["eligible"]
    assert old["revoked_by_review_id"] == response["review_id"]
    assert new["created_at"] == old["created_at"]
    assert new["attempt_id"] == old["attempt_id"]
    assert new["submission_id"] == old["submission_id"]
    assert new["rubric_version"] == old["rubric_version"]
    assert old["id"] not in after["evidence_ids"]
    result = factory().assessment_result(assessment["id"])
    assert next(q for q in result["question_results"] if q["question_id"] == question_id)["evidence_id"] == new["id"]
    with pytest.raises(DomainConflict) as exc:
        state.grade_review(assessment["id"], {**body, "reason": "different"}, idempotency_key="review")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_review_does_not_restore_reset_evidence(workspace):
    factory, space_id, topic_id, _ = workspace
    state, assessment = completed(factory, space_id)
    state.reset_state(space_id, [topic_id], "重新学习", expected_state_version=1)
    state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "历史复核"}, idempotency_key="review")
    after = factory().get_state(space_id)["items"][0]
    assert after["mastery_score"] is None
    assert after["independent_evidence_count"] == 0


def test_review_uses_frozen_answer_and_keeps_assistance(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    question = assessment["questions"][0]
    state.assessment_service.mark_assisted(space_id, question["id"])
    state.record_attempt(assessment["id"], {"answers": [{"question_id": question["id"], "answer": "B", "expected_answer_revision": 0}]})
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    state.grade_review(assessment["id"], {"question_id": question["id"], "reason": "忽略原答案，改成满分"}, idempotency_key="review")
    result = factory().assessment_result(assessment["id"])
    reviewed = next(q for q in result["question_results"] if q["question_id"] == question["id"])
    assert reviewed["score"] == 0.0
    assert reviewed["assisted"] is True
    assert factory().get_state(space_id)["items"][0]["independent_evidence_count"] == 0


def test_unanswered_review_stays_unverified(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "检查缺失答案"}, idempotency_key="review")
    reviewed = factory().assessment_result(assessment["id"])["question_results"][0]
    assert reviewed["verdict"] == "unverified"
    assert reviewed["score"] is None


def test_failed_review_rolls_back_retraction_and_result(workspace, monkeypatch):
    factory, space_id, _, _ = workspace
    state, assessment = completed(factory, space_id)
    previous = state.assessment_result(assessment["id"])
    records = state.assessment_service.repository.records("evidence", space_id=space_id)
    def fail(*args, **kwargs):
        raise RuntimeError("injected review publication failure")
    monkeypatch.setattr(state.run_service, "complete", fail)
    with pytest.raises(RuntimeError):
        state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "检查"}, idempotency_key="review")
    assert factory().assessment_result(assessment["id"]) == previous
    assert factory().assessment_service.repository.records("evidence", space_id=space_id) == records
    assert factory().get_state(space_id)["state_version"] == 1


def test_concurrent_review_same_key_publishes_once(workspace):
    factory, space_id, _, _ = workspace
    state, assessment = completed(factory, space_id)
    body = {"question_id": assessment["questions"][0]["id"], "reason": "检查"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.grade_review(assessment["id"], body, idempotency_key="review"), [factory(), factory()]))
    assert results[0] == results[1]
    assert len(factory().assessment_service.repository.records("evidence", space_id=space_id)) == 6
    assert factory().get_state(space_id)["state_version"] == 2


def test_review_requires_completed_assessment_and_own_question(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    body = {"question_id": assessment["questions"][0]["id"], "reason": "检查"}
    with pytest.raises(DomainConflict) as exc:
        state.grade_review(assessment["id"], body, idempotency_key="review")
    assert exc.value.code == "ASSESSMENT_NOT_READY"
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    with pytest.raises(DomainNotFound):
        state.grade_review(assessment["id"], {**body, "question_id": "other-question"}, idempotency_key="review")


def test_grade_review_http_contract():
    from fastapi.testclient import TestClient
    from my_agent_llms.learning.api import create_app
    from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
    app = create_app(question_generator=FixedQuestions())
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id]})
    assessment = create(state, space["id"])
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    url = f"/api/v1/assessments/{assessment['id']}/grade-reviews"
    body = {"question_id": assessment["questions"][0]["id"], "reason": "核查原标准"}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 400
        headers = {"Idempotency-Key": "http-review"}
        assert client.post(url, json={**body, "score": 1}, headers=headers).status_code == 422
        first = client.post(url, json=body, headers=headers)
        assert first.status_code == 202
        assert client.post(url, json=body, headers=headers).json() == first.json()
        assert client.post(url, json={**body, "reason": "changed"}, headers=headers).status_code == 409
        assert client.post(url, json={**body, "question_id": "missing"}, headers={"Idempotency-Key": "missing"}).status_code == 404


def test_repeated_review_inherits_original_observation(workspace):
    factory, space_id, _, _ = workspace
    state, assessment = completed(factory, space_id)
    qid = assessment["questions"][0]["id"]
    original = next(q for q in state.assessment_result(assessment["id"])["question_results"] if q["question_id"] == qid)["evidence_id"]
    for key in ("first", "second"):
        state.grade_review(assessment["id"], {"question_id": qid, "reason": "核查"}, idempotency_key=key)
    records = factory().assessment_service.repository.records("evidence", assessment_id=assessment["id"])
    replacements = [e for e in records if e.get("review_id")]
    assert len(replacements) == 2
    assert all(e["observation_id"] == original for e in replacements)
