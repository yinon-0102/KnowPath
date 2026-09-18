from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
from fastapi.testclient import TestClient

from my_agent_llms.learning.api import create_app


def test_learning_api_workflow_from_material_to_plan():
    client = TestClient(create_app(question_generator=FixedQuestions()))
    material = client.post(
        "/api/v1/materials",
        files={"file": ("python.md", b"# Functions\n\nReusable behavior.", "text/markdown")},
        headers={"Idempotency-Key": "material-1"},
    ).json()
    material_id = material["material"]["id"]

    space = client.post(
        "/api/v1/learning-spaces",
        json={"name": "Python", "material_ids": [material_id], "goal": "Use functions"},
        headers={"Idempotency-Key": "space-1"},
    ).json()
    space_id = space["id"]
    topics = client.get(f"/api/v1/materials/{material_id}/topics").json()["items"]
    scope = client.post(
        f"/api/v1/learning-spaces/{space_id}/scope",
        json={"topic_ids": [topics[0]["id"]], "expected_version": 1},
        headers={"Idempotency-Key": "scope-1"},
    )
    assert scope.status_code == 200

    created = client.post(
        f"/api/v1/learning-spaces/{space_id}/assessments",
        json={"kind": "diagnostic", "question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}},
        headers={"Idempotency-Key": "assessment-1"},
    )
    assert created.status_code == 202
    assessment_id = created.json()["assessment_id"]
    assessment = client.get(f"/api/v1/assessments/{assessment_id}").json()
    question_id = assessment["questions"][0]["id"]

    assert client.post(
        f"/api/v1/assessments/{assessment_id}/attempts",
        json={"answers": [{"question_id": question_id, "answer": "A", "expected_answer_revision": 0}]},
        headers={"Idempotency-Key": "attempt-1"},
    ).status_code == 200
    assert client.post(
        f"/api/v1/assessments/{assessment_id}/finalize",
        json={"allow_unanswered": True},
    ).status_code == 202
    assert client.get(f"/api/v1/assessments/{assessment_id}/result").status_code == 200

    plan = client.post(
        f"/api/v1/learning-spaces/{space_id}/plans",
        json={"session_count": 3, "minutes_per_session": 30},
        headers={"Idempotency-Key": "plan-1"},
    )
    assert plan.status_code == 202
    plan_id = plan.json()["plan_id"]
    plan_body = client.get(f"/api/v1/plans/{plan_id}").json()
    session = client.post(
        f"/api/v1/plans/{plan_id}/sessions",
        json={"task_id": plan_body["tasks"][0]["id"]},
        headers={"Idempotency-Key": "session-1"},
    )
    assert session.status_code == 201
    session_id = session.json()["id"]
    assert client.post(f"/api/v1/sessions/{session_id}/finish", json={}).status_code == 200


def test_message_events_publish_text_before_terminal():
    from my_agent_llms.test.test_learning_state_persistence import FixedAnswer
    app = create_app(answer_generator=FixedAnswer())
    with TestClient(app) as client:
        material = client.post(
            "/api/v1/materials",
            files={"file": ("notes.md", b"# Functions\n\nReusable behavior.", "text/markdown")},
            headers={"Idempotency-Key": "message-material"},
        ).json()
        space = client.post(
            "/api/v1/learning-spaces",
            json={"name": "Python", "material_ids": [material["material"]["id"]]},
            headers={"Idempotency-Key": "message-space"},
        ).json()
        response = client.post(
            f"/api/v1/learning-spaces/{space['id']}/messages",
            json={"message": "Explain functions", "stream": True},
            headers={"Idempotency-Key": "message-send"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        stream = client.get(f"/api/v1/runs/{run_id}/events")
        names = [line.removeprefix("event: ") for line in stream.text.splitlines() if line.startswith("event: ")]
        assert names == ["run.started", "tool.completed", "message.delta", "message.completed", "run.completed"]
        run = app.state.learning_state.get_run(run_id)
        assert run["events"][3]["data"]["message_id"] == run["result_ref"]["id"]
        assert "citations" in run["events"][3]["data"]
