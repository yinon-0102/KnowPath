"""Confirmed space deletion removes owned records while preserving shared material."""
import pytest
from fastapi.testclient import TestClient

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.errors import DomainConflict, DomainNotFound
from my_agent_llms.test.test_learning_state_persistence import workspace, create, answer


def confirmed(state, space_id):
    return {"confirm": True, "expected_version": state.get_space(space_id)["space_version"]}


def test_delete_space_removes_history_and_keeps_shared_material(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    material_id = state.get_space(space_id)["bindings"][0]["material_id"]
    other = state.create_space({"name": "Other", "material_ids": [material_id]})
    try:
        assessment = create(state, space_id)
        answer(state, assessment)
        state.finalize_assessment(assessment["id"], {})
        review = state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "review"})
        plan = state.create_plan(space_id, {}, idempotency_key=space_id + "-plan")
        session = state.start_session(plan["id"], plan["tasks"][0]["id"])
        state.add_session_event(session["id"], {"type": "pause", "event_id": session["id"]})
        export = state.create_export(space_id)
        result = state.delete_space(space_id, confirmed(state, space_id))
        restored = factory()
        assert result["status"] == "succeeded"
        assert restored.get_run(result["run_id"])["status"] == "succeeded"
        for loader, identifier in ((restored.get_space, space_id), (restored.get_assessment, assessment["id"]),
                                   (restored.get_plan, plan["id"]), (restored.finish_session, session["id"]),
                                   (restored.export_payload, export["export_id"]), (restored.get_run, review["run_id"])):
            with pytest.raises(DomainNotFound):
                loader(identifier)
        for table in ("states", "evidence", "resets", "assessments", "plans", "sessions"):
            assert restored.assessment_service.repository.records(table, space_id=space_id) == []
        assert restored.material_repository.get_material(material_id) is not None
        assert restored.topics_for_space(other["id"])
        with pytest.raises(DomainConflict) as error:
            restored.create_plan(space_id, {}, idempotency_key=space_id + "-plan")
        assert error.value.code == "RESOURCE_DELETED"
    finally:
        state.delete_space(other["id"], confirmed(state, other["id"]))


def test_delete_failure_rolls_back_records_and_run(workspace, monkeypatch):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    original = state.run_service.create
    def fail(kind, *args, **kwargs):
        if kind == "space_delete":
            raise RuntimeError("delete run failed")
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(state.run_service, "create", fail)
    with pytest.raises(RuntimeError, match="delete run failed"):
        state.delete_space(space_id, confirmed(state, space_id))
    assert factory().get_space(space_id)["id"] == space_id
    assert factory().get_assessment(assessment["id"])["id"] == assessment["id"]
    assert factory().get_run(assessment["run_id"])["status"] == "succeeded"


def test_delete_http_requires_confirmation_and_current_version():
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material").material
    space = state.create_space({"name": "Delete", "material_ids": [material.id]})
    url = f"/api/v1/learning-spaces/{space['id']}"
    with TestClient(app) as client:
        for body in ({}, {"expected_version": 1}, {"confirm": False, "expected_version": 1}, {"confirm": "true", "expected_version": 1}):
            assert client.request("DELETE", url, json=body).status_code == 422
        assert client.request("DELETE", url, json={"confirm": True, "expected_version": 99}).status_code == 409
        response = client.request("DELETE", url, json={"confirm": True, "expected_version": 1})
        assert response.status_code == 202
        assert response.json()["status"] == "succeeded"
        assert client.get(url).status_code == 404


def test_late_generation_does_not_recreate_deleted_records(workspace):
    from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
    factory, space_id, _, _ = workspace
    state = factory()
    class DeleteDuringGeneration(FixedQuestions):
        def generate(self, topics, request):
            factory().delete_space(space_id, confirmed(factory(), space_id))
            return super().generate(topics, request)
    state.assessment_service.generator = DeleteDuringGeneration()
    response = state.create_assessment(space_id, {"kind": "diagnostic", "question_count": 5,
        "question_types": ["single_choice"], "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}},
        dispatch=lambda *args: None)
    state.assessment_service.generate(response["id"])
    state.assessment_service.generate(response["id"])
    with pytest.raises(DomainNotFound):
        factory().get_assessment(response["id"])


def test_delete_removes_message_runs_after_restart(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    message = state.send_message(space_id, {"message": "private content to remove"})
    factory().delete_space(space_id, confirmed(factory(), space_id))
    for run_id in (message["run_id"],):
        with pytest.raises(DomainNotFound):
            factory().get_run(run_id)
        with pytest.raises(DomainNotFound):
            factory().events_for(run_id)
    with pytest.raises(DomainNotFound):
        state.send_message(space_id, {"message": "late message"})


def test_mysql_delete_waits_for_message_transaction(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    factory, space_id, _, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("requires MySQL locks")
    writer, deleter = factory(), factory()
    body = confirmed(deleter, space_id)
    entered, release, started = Event(), Event(), Event()
    original = writer.run_service.append_event
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(writer.run_service, "append_event", held)
    def remove():
        started.set()
        return deleter.delete_space(space_id, body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(writer.send_message, space_id, {"message": "in flight"})
        try:
            assert entered.wait(5)
            deleting = pool.submit(remove)
            assert started.wait(5)
        finally:
            release.set()
        message = sending.result(timeout=15)
        deleting.result(timeout=15)
    with pytest.raises(DomainNotFound):
        factory().get_run(message["run_id"])
