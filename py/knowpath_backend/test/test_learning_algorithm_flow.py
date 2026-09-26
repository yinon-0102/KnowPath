"""Exercise all four surfaces against the same real in-process API/state store."""
from fastapi.testclient import TestClient

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.retrieval import KeywordRetriever
from knowpath_backend.test.test_learning_adaptive_diagnostics import adaptive_workspace, TopicQuestions


def test_four_features_share_one_authenticated_learning_history(adaptive_workspace):
    factory, space_id, topics = adaptive_workspace
    original = factory()
    app = create_app(service=original.material_service,
        settings=LearningSettings(local_token="algorithm-flow-token"),
        question_generator=TopicQuestions(salt="end-to-end"), source_retriever=KeywordRetriever())
    auth = {"X-Local-Token": "algorithm-flow-token"}
    def command(client, path, body, key):
        result = client.post(path, json=body, headers={**auth, "Idempotency-Key": key})
        assert result.status_code in {200, 202}, result.text
        return result.json()
    with TestClient(app) as client:
        base = f"/api/v1/learning-spaces/{space_id}"
        created = command(client, base + "/assessments", {
            "kind": "diagnostic", "adaptive": True, "question_count": 5,
            "question_types": ["single_choice"],
        }, "flow-create")
        assessment_id = created["assessment_id"]
        path = f"/api/v1/assessments/{assessment_id}"
        question_ids = []
        for step in range(6):
            response = client.get(path + "/diagnostic", headers=auth)
            assert response.status_code == 200, response.text
            diagnostic = response.json()
            assert not any(secret in str(diagnostic) for secret in ("answer_key", "rubric", "source_text"))
            question = diagnostic["current_question"]
            if question is None:
                break
            assert question["id"] not in question_ids
            question_ids.append(question["id"])
            command(client, path + "/attempts", {"answers": [{
                "question_id": question["id"], "expected_answer_revision": 0,
                "answer": "B" if step == 0 else "A",
            }]}, f"flow-answer-{step}")
        assert diagnostic["completion_reason"] == "target_reached"
        assert len(question_ids) == 5
        command(client, path + "/finalize", {"allow_unanswered": False}, "flow-finalize")
        state = app.state.learning_state
        before = state.get_state(space_id)
        evolution = client.get(base + "/evolution", headers=auth)
        assert evolution.status_code == 200
        assert evolution.json()["policy_version"] == "review-v2"
        assert set(item["topic_id"] for item in evolution.json()["items"]) == set(topics.values())
        assert any(item["curve"] for item in evolution.json()["items"])
        comparison = command(client, base + "/plan-comparisons", {
            "budgets_minutes_per_day": [20, 40], "horizon_days": 7,
        }, "flow-comparison")
        assert [scenario["minutes_per_day"] for scenario in comparison["scenarios"]] == [20, 40]
        assert all(day["minutes"] <= scenario["minutes_per_day"]
            for scenario in comparison["scenarios"] for day in scenario["daily_totals"])
        replay = command(client, base + "/policy-replays", {}, "flow-replay")
        assert assessment_id in {decision["assessment_id"] for decision in replay["decisions"]}
        assert len(replay["policies"]) == 3 and replay["causal_effect_estimated"] is False
        assert replay["observed_retests"]["pair_count"] == 0  # Same-session practice is not a delayed retest.
        assert state.get_state(space_id) == before
        assert state.assessment_service.repository.records("plans", space_id=space_id) == []
