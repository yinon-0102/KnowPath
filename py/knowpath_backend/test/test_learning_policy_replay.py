"""Historical decisions use only information available at their creation time."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from knowpath_backend.learning.evaluation.replay import build_replay, evidence_as_of, ReplayService
from knowpath_backend.learning.evaluation.schemas import ReplayRequest
from knowpath_backend.test.test_learning_state_persistence import workspace, create, answer


def timestamp(day, hour=0):
    return f"2026-09-{day:02d}T{hour:02d}:00:00+00:00"


def observation(identifier, day, *, topic="a", score=1, **extra):
    return dict(id=identifier, space_id="s", assessment_id=identifier, question_id=identifier,
                topic_id=topic, topic_revision_id="r1", epoch=0, score=score,
                eligible=True, assisted=False, family_id=identifier, error_tags=[],
                created_at=timestamp(day), observed_at=timestamp(day),
                is_application=True, difficulty="medium", **extra)


def assessment(identifier="target", day=5, *, epochs=None, revision="r1"):
    return dict(id=identifier, space_id="s", status="completed", created_at=timestamp(day),
                result={"graded_at": timestamp(day, 1)},
                snapshot={"topics": [{"id": "b", "revision_id": revision, "prerequisites": ["a"]},
                                     {"id": "a", "revision_id": revision, "prerequisites": []}],
                          "epochs": epochs or {}, "scope_version": 1, "bindings": []})


def run(assessments, rows, **kwargs):
    return build_replay(assessments, rows, as_of=timestamp(26), **kwargs)


def test_empty_history_is_unknown_not_zero_gain():
    result = run([], [])
    assert result["prediction"]["mean_absolute_error"] is None
    assert result["observed_retests"]["mean_score_change"] is None
    assert result["observed_retests"]["pairs"] == []
    assert len(result["policies"]) == 3
    assert result["causal_effect_estimated"] is False


def test_future_evidence_and_target_answers_do_not_change_decisions():
    before = observation("before", 2, score=0)
    baseline = run([assessment()], [before])
    future = observation("future", 8, topic="b")
    target = observation("own", 6, topic="b")
    target["assessment_id"] = "target"
    result = run([assessment()], [before, future, target])
    assert result["decisions"] == baseline["decisions"]
    assert result["decisions"][0]["candidate_topic_ids"] == ["b", "a"]
    assert result["prediction"]["predicted_observations"] == 0


def test_future_correction_cannot_rewrite_prefix_state():
    original = observation("original", 2, score=0)
    before = run([assessment()], [original])["decisions"]
    old = dict(original, eligible=False, eligibility_before_revocation=True,
               revoked_at=timestamp(9), revoked_by_review_id="review")
    replacement = dict(original, id="new", score=1, reviewed_at=timestamp(9),
                       replaces_evidence_id="original", observation_id="original")
    assert run([assessment()], [old, replacement])["decisions"] == before
    assert evidence_as_of([old, replacement], timestamp(10))[0]["id"] == "new"


def test_same_time_correction_chain_uses_lineage_not_uuid_order():
    first = observation("z", 2, score=0)
    first.update(eligible=False, eligibility_before_revocation=True, revoked_at=timestamp(3), revoked_by_review_id="r")
    second = dict(first, id="y", reviewed_at=timestamp(3), replaces_evidence_id="z", observation_id="z")
    final = dict(second, id="a", score=1, eligible=True, replaces_evidence_id="y")
    final.pop("revoked_at"); final.pop("revoked_by_review_id")
    assert [r["id"] for r in evidence_as_of([final, first, second], timestamp(4))] == ["a"]


@pytest.mark.parametrize("override", [{"assisted": True}, {"eligible": False}, {"epoch": 1},
    {"topic_revision_id": "old"}, {"score": None}, {"question_review_status": "pending"}])
def test_invalid_observations_do_not_predict_or_influence_choices(override):
    row = observation("old", 2); row.update(override)
    result = run([assessment()], [row])
    assert result["decisions"][0]["states"]["a"]["mastery_score"] is None


def test_predictions_are_separate_from_policy_outcomes():
    old = observation("old", 2, score=0)
    target = observation("answer", 6); target["assessment_id"] = "target"
    result = run([assessment()], [old, target])
    assert result["prediction"]["mean_absolute_error"] == 1
    assert result["prediction"]["predicted_observations"] == 1
    assert all("learning_gain" not in policy for policy in result["policies"])


def test_retests_need_distinct_family_assessment_and_real_answer_delay():
    baseline = observation("base", 2, score=0)
    good = observation("good", 5)
    late_graded = observation("late", 8, score=0); late_graded["observed_at"] = timestamp(2, 1)
    repeat = observation("repeat", 6); repeat["family_id"] = "base"
    assisted = observation("assist", 7); assisted["assisted"] = True
    reset = observation("reset", 9); reset["epoch"] = 2
    changed = observation("changed", 10); changed["topic_revision_id"] = "r2"
    report = run([], [baseline, good, late_graded, repeat, assisted, reset, changed])["observed_retests"]
    assert report["pair_count"] == 1
    assert report["pairs"][0]["followup_evidence_id"] == "good"
    assert report["mean_score_change"] == 1


def test_unknown_actual_observation_time_is_not_claimed_as_delayed_retest():
    first, second = observation("first", 2), observation("second", 9)
    first.pop("observed_at"); second.pop("observed_at")
    assert run([], [first, second])["observed_retests"]["pair_count"] == 0


def test_scopes_epochs_limits_and_private_text_are_preserved():
    old, current = assessment("old", 3), assessment("current", 10, epochs={"a": 2})
    current["snapshot"]["topics"][0]["source_text"] = "secret-answer-key"
    rows = [observation("old-a", 2)]
    result = run([old, current], rows, limit=1)
    assert result["truncated"] is True
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["assessment_id"] == "current"
    assert result["decisions"][0]["states"]["a"]["mastery_score"] is None
    assert "secret-answer-key" not in str(result)


@pytest.mark.parametrize("payload", [{"limit": True}, {"limit": 1001}, {"minimum_delay_hours": 0},
    {"from_time": "2026-09-01"}, {"from_time": timestamp(9), "to_time": timestamp(2)}, {"surprise": 1}])
def test_strict_request_rejects_ambiguous_or_unbounded_inputs(payload):
    with pytest.raises(ValidationError):
        ReplayRequest.model_validate(payload)


def test_service_replay_is_durable_read_only_and_private(workspace):
    factory, space_id, _, _ = workspace
    state = factory(); assessment_row = create(state, space_id)
    answer(state, assessment_row, finalize=True)
    repo = state.assessment_service.repository
    tables = ("evidence", "assessments", "states", "plans", "tasks")
    before = {name: deepcopy(repo.records(name, **({} if name == "tasks" else {"space_id": space_id}))) for name in tables}
    result = ReplayService(state.assessment_service).replay(space_id, {}, "report")
    retry = ReplayService(factory().assessment_service).replay(space_id, {}, "report")
    assert result == retry
    assert result["decisions"][0]["assessment_id"] == assessment_row["id"]
    assert {name: repo.records(name, **({} if name == "tasks" else {"space_id": space_id})) for name in tables} == before
    assert "answer_key" not in str(result) and "source_text" not in str(result)


def test_original_ineligible_grade_is_not_revived_before_later_correction():
    original = observation("old", 2)
    original.update(eligible=False, eligibility_before_revocation=False,
        revoked_at=timestamp(9), revoked_by_review_id="review")
    replacement = dict(original, id="new", reviewed_at=timestamp(9), replaces_evidence_id="old")
    assert run([assessment()], [original, replacement])["decisions"][0]["states"]["a"]["mastery_score"] is None


def test_before_correction_report_uses_old_outcome_not_future_new_grade():
    original = observation("old", 2, score=0)
    original.update(eligible=False, eligibility_before_revocation=True,
        revoked_at=timestamp(9), revoked_by_review_id="review")
    replacement = dict(original, id="new", score=1, eligible=True,
        reviewed_at=timestamp(9), replaces_evidence_id="old", observation_id="old")
    replacement.pop("revoked_by_review_id"); replacement.pop("revoked_at")
    later = observation("later", 5)
    result = run([], [original, replacement, later], to_time=timestamp(6))
    assert result["observed_retests"]["mean_score_change"] == 1
    assert run([], [original, replacement, later])["observed_retests"]["mean_score_change"] == 0


@pytest.mark.parametrize("status", ["generating", "ready", "in_progress", "stale", "cancelled", "failed"])
def test_only_completed_assessments_enter_historical_policy_comparisons(status):
    row = assessment(); row["status"] = status
    assert run([row], [])["decisions"] == []


def test_late_completion_cannot_enter_an_earlier_report():
    row = assessment(); row["result"]["graded_at"] = timestamp(10)
    assert run([row], [], to_time=timestamp(6))["decisions"] == []
    row["result"].pop("graded_at")
    assert run([row], [])["decisions"] == []


def test_intermediate_independent_practice_restarts_retest_spacing():
    first = observation("first", 1, score=0)
    intermediate = observation("intermediate", 1, score=0)
    intermediate["observed_at"] = intermediate["created_at"] = timestamp(1, 23)
    followup = observation("followup", 2)
    assert run([], [first, intermediate, followup])["observed_retests"]["pair_count"] == 0


@pytest.mark.parametrize("override", [{"assisted": True}, {"family_id": "first"}])
def test_known_assistance_or_repeated_practice_breaks_retest_pairs(override):
    first = observation("first", 1, score=0)
    intermediate = observation("intermediate", 2, score=0); intermediate.update(override)
    followup = observation("followup", 3)
    assert run([], [first, intermediate, followup])["observed_retests"]["pair_count"] == 0


def test_combined_history_and_candidate_work_is_bounded():
    from knowpath_backend.learning.errors import DomainConflict
    assessments = [assessment(str(i)) for i in range(20)]
    for item in assessments:
        item["snapshot"]["topics"] = [{"id": str(i), "revision_id": "r1", "prerequisites": []} for i in range(100)]
    rows = [observation(str(i), 1, topic=str(i % 100)) for i in range(2000)]
    with pytest.raises(DomainConflict) as error:
        run(assessments, rows)
    assert error.value.code == "REPLAY_LIMIT_EXCEEDED"


def test_replay_route_auth_scope_validation_and_same_key():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.rag.retrieval import KeywordRetriever
    app = create_app(settings=LearningSettings(local_token="test-token"), source_retriever=KeywordRetriever())
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Topic\n\nA grounded topic.", idempotency_key="material")
    space = state.create_space({"name": "Replay", "material_ids": [material.material.id]})
    path = f"/api/v1/learning-spaces/{space['id']}/policy-replays"
    headers = {"X-Local-Token": "test-token", "Idempotency-Key": "report"}
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 401
        assert client.post(path, json={}, headers={"X-Local-Token": "test-token"}).status_code == 400
        assert client.post(path, json={"limit": True}, headers=headers).status_code == 422
        first = client.post(path, json={}, headers=headers)
        assert first.status_code == 200
        assert client.post(path, json={}, headers=headers).json() == first.json()
        assert client.post(path, json={"limit": 2}, headers=headers).status_code == 409
        assert client.post("/api/v1/learning-spaces/missing/policy-replays", json={},
            headers={**headers, "Idempotency-Key": "missing"}).status_code == 404
