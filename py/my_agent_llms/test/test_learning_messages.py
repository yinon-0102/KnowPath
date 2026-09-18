"""Grounded, durable messages share the learning command unit of work."""
import copy
import pytest
from my_agent_llms.learning.errors import DomainConflict, DomainNotFound
from my_agent_llms.test.test_learning_state_persistence import workspace, create


class FixedAnswer:
    def __init__(self):
        self.calls = []
    def generate(self, snapshot):
        self.calls.append(copy.deepcopy(snapshot))
        return {"text": "Functions group reusable behavior.", "citation_ids": [snapshot["sources"][0]["chunk_id"]]}


def service(factory):
    state = factory()
    generator = FixedAnswer()
    state.message_service.generator = generator
    return state, generator


def test_message_replay_restart_sources_and_standalone_session(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    body = {"message": "Explain functions", "stream": True}
    first = state.send_message(space_id, body, idempotency_key="message-one")
    again, other_generator = service(factory)
    assert again.send_message(space_id, body, idempotency_key="message-one") == first
    assert len(generator.calls) == 1 and other_generator.calls == []
    assert first["session_id"]
    events = again.events_for(first["run_id"])
    completed = next(e["data"] for e in events if e["event"] == "message.completed")
    assert completed["text"] == "Functions group reusable behavior."
    ref = completed["citations"][0]
    binding = state.get_space(space_id)["bindings"][0]
    assert ref["material_version_id"] == binding["material_version_id"]
    assert ref["chunk_id"] == generator.calls[0]["sources"][0]["chunk_id"]
    assert any(e["event"] == "tool.completed" for e in events)
    assert again.get_state(space_id)["state_version"] == 0
    again.send_message(space_id, {**body, "session_id": first["session_id"]}, idempotency_key="message-two")
    assert len(other_generator.calls[0]["history"]) == 2
    with pytest.raises(DomainConflict) as error:
        again.send_message(space_id, {**body, "message": "different"}, idempotency_key="message-one")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_message_rejects_invented_citations_without_publishing_text(workspace):
    factory, space_id, _, _ = workspace
    state, _ = service(factory)
    state.message_service.generator.generate = lambda snapshot: {"text": "invented", "citation_ids": ["foreign-chunk"]}
    message = state.send_message(space_id, {"message": "Explain functions"})
    run = factory().get_run(message["run_id"])
    assert run["status"] == "failed"
    assert run["error"]["code"] == "MESSAGE_VALIDATION_FAILED"
    assert not any(e["event"].startswith("message.") for e in run["events"])


def test_message_scope_change_discards_model_output(workspace):
    factory, space_id, topic_id, _ = workspace
    state, generator = service(factory)
    original = generator.generate
    def change(snapshot):
        other = factory()
        other.set_scope(space_id, {"topic_ids": [topic_id], "expected_version": other.get_space(space_id)["space_version"]})
        return original(snapshot)
    generator.generate = change
    response = state.send_message(space_id, {"message": "Explain functions"})
    run = factory().get_run(response["run_id"])
    assert run["status"] == "failed"
    assert run["error"]["code"] == "STALE_LEARNING_CONTEXT"


def test_active_question_request_becomes_hint_without_model_or_score(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    assessment = create(state, space_id)
    question = assessment["questions"][0]
    response = state.send_message(space_id, {"message": "Give the answer for " + question["id"]})
    assert generator.calls == []
    stored = factory().assessment_service.repository.get_record("assessments", assessment["id"])
    assert stored["questions"][0]["assisted"] is True
    assert factory().get_state(space_id)["state_version"] == 0
    assert factory().get_run(response["run_id"])["status"] == "succeeded"


def test_message_cancel_and_delete_ignore_late_output(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    response = state.send_message(space_id, {"message": "Explain functions"}, dispatch=lambda *args: None)
    state.cancel_run(response["run_id"])
    record = state.assessment_service.repository.records("messages", space_id=space_id)[0]
    state.message_service.generate(record["id"])
    assert generator.calls == []
    assert factory().get_run(response["run_id"])["status"] == "cancelled"
    response = state.send_message(space_id, {"message": "Explain again"}, dispatch=lambda *args: None)
    record = state.assessment_service.repository.records("messages", space_id=space_id)[-1]
    state.delete_space(space_id, {"confirm": True, "expected_version": state.get_space(space_id)["space_version"]})
    state.message_service.generate(record["id"])
    assert factory().assessment_service.repository.records("messages", space_id=space_id) == []
    with pytest.raises(DomainNotFound):
        factory().get_run(response["run_id"])


def test_history_uses_submission_order_even_with_equal_database_timestamps(workspace, monkeypatch):
    from uuid import UUID
    import my_agent_llms.learning.messages as module
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    ids = iter(str(UUID(int=n)) for n in (100, 90, 80, 70))
    monkeypatch.setattr(module, "uid", lambda: next(ids))
    monkeypatch.setattr(module, "now", lambda: "2026-09-19T00:00:00+00:00")
    first = state.send_message(space_id, {"message": "first explanation"})
    state.send_message(space_id, {"message": "second explanation", "session_id": first["session_id"]})
    state.send_message(space_id, {"message": "third explanation", "session_id": first["session_id"]})
    assert [m["content"] for m in generator.calls[-1]["history"] if m["role"] == "user"] == ["first explanation", "second explanation"]


def test_message_delete_during_model_call_never_recreates_content(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    original = generator.generate
    def remove(snapshot):
        other = factory()
        other.delete_space(space_id, {"confirm": True, "expected_version": other.get_space(space_id)["space_version"]})
        return original(snapshot)
    generator.generate = remove
    response = state.send_message(space_id, {"message": "Explain functions"}, idempotency_key="delete-during")
    assert factory().assessment_service.repository.records("messages", space_id=space_id) == []
    with pytest.raises(DomainNotFound):
        factory().get_run(response["run_id"])
    with pytest.raises(DomainConflict) as error:
        factory().send_message(space_id, {"message": "Explain functions"}, idempotency_key="delete-during")
    assert error.value.code == "RESOURCE_DELETED"


def test_message_cancel_during_model_call_and_duplicate_worker(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    jobs = []
    response = state.send_message(space_id, {"message": "Explain functions"}, dispatch=lambda fn, ident: jobs.append((fn, ident)))
    original = generator.generate
    def cancel(snapshot):
        factory().cancel_run(response["run_id"])
        return original(snapshot)
    generator.generate = cancel
    fn, ident = jobs[0]
    fn(ident)
    fn(ident)
    run = factory().get_run(response["run_id"])
    assert len(generator.calls) == 1
    assert run["status"] == "cancelled"
    assert not any(e["event"].startswith("message.") for e in run["events"])


def test_stale_history_is_not_sent_to_model(workspace):
    factory, space_id, topic_id, _ = workspace
    state, generator = service(factory)
    first = state.send_message(space_id, {"message": "Explain functions"})
    state.set_scope(space_id, {"topic_ids": [topic_id], "expected_version": state.get_space(space_id)["space_version"]})
    state.send_message(space_id, {"message": "Explain new scope", "session_id": first["session_id"]})
    assert generator.calls[-1]["history"] == []


def test_learning_session_finishing_during_generation_discards_output(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    plan = state.create_plan(space_id, {})
    session = state.start_session(plan["id"], plan["tasks"][0]["id"])
    original = generator.generate
    def finish(snapshot):
        factory().finish_session(session["id"])
        return original(snapshot)
    generator.generate = finish
    try:
        response = state.send_message(space_id, {"message": "Explain", "session_id": session["id"]})
        run = factory().get_run(response["run_id"])
        assert run["status"] == "failed"
        assert run["error"]["code"] == "SESSION_FINISHED"
        assert not any(e["event"].startswith("message.") for e in run["events"])
        with pytest.raises(DomainConflict) as exc:
            state.send_message(space_id, {"message": "Again", "session_id": session["id"]})
        assert exc.value.code == "SESSION_FINISHED"
    finally:
        state.delete_space(space_id, {"confirm": True, "expected_version": state.get_space(space_id)["space_version"]})


def test_session_isolation_and_pending_message_exclusion(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    first = state.send_message(space_id, {"message": "Explain"}, dispatch=lambda *args: None)
    with pytest.raises(DomainConflict) as exc:
        state.send_message(space_id, {"message": "Another", "session_id": first["session_id"]})
    assert exc.value.code == "MESSAGE_IN_PROGRESS"
    other = state.create_space({"name": "Other", "material_ids": [state.get_space(space_id)["bindings"][0]["material_id"]]})
    try:
        with pytest.raises(DomainNotFound):
            state.send_message(other["id"], {"message": "Explain", "session_id": first["session_id"]})
        independent = state.send_message(space_id, {"message": "Independent"})
        assert independent["session_id"] != first["session_id"]
        assert generator.calls[-1]["history"] == []
    finally:
        state.delete_space(other["id"], {"confirm": True, "expected_version": state.get_space(other["id"])["space_version"]})


def test_restart_marks_inflight_run_interrupted_without_retrying_model(workspace, monkeypatch):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    jobs = []
    first = state.send_message(space_id, {"message": "Explain"}, idempotency_key="interrupt", dispatch=lambda fn, ident: jobs.append((fn, ident)))
    recovering = factory().run_service
    # Shared MySQL may contain real work; emulate startup only for this fixture.
    monkeypatch.setattr(recovering.repository, "active_ids", lambda: [first["run_id"]])
    recovering.recover_interrupted()
    jobs[0][0](jobs[0][1])
    assert generator.calls == []
    assert factory().get_run(first["run_id"])["error"]["code"] == "RUN_INTERRUPTED"
    assert state.send_message(space_id, {"message": "Explain"}, idempotency_key="interrupt") == first
    state.send_message(space_id, {"message": "Retry", "session_id": first["session_id"]})
    assert len(generator.calls) == 1


def test_message_preparation_failure_rolls_back_run_and_conversation(workspace, monkeypatch):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    ids = []
    original = state.run_service.create
    def track(*args, **kwargs):
        result = original(*args, **kwargs)
        ids.append(result["id"])
        return result
    monkeypatch.setattr(state.run_service, "create", track)
    def fail(*args, **kwargs):
        raise RuntimeError("injected transaction failure")
    monkeypatch.setattr(state.run_service, "append_event", fail)
    with pytest.raises(RuntimeError):
        state.send_message(space_id, {"message": "Explain"}, idempotency_key="rollback-message")
    assert ids and generator.calls == []
    for table in ("messages", "conversations"):
        assert factory().assessment_service.repository.records(table, space_id=space_id) == []
    for identifier in ids:
        with pytest.raises(DomainNotFound):
            factory().get_run(identifier)
    fresh, _ = service(factory)
    assert fresh.send_message(space_id, {"message": "Explain"}, idempotency_key="rollback-message")["run_id"] not in ids


def test_mysql_same_key_concurrent_requests_dispatch_once(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    factory, space_id, _, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("requires MySQL transaction scheduling")
    barrier = Barrier(2)
    jobs = []
    def send():
        state, _ = service(factory)
        barrier.wait(timeout=5)
        return state.send_message(space_id, {"message": "Explain"}, idempotency_key="concurrent-message", dispatch=lambda *args: jobs.append(args))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send) for _ in range(2)]
        responses = [future.result(timeout=15) for future in futures]
    assert responses[0] == responses[1]
    assert len(jobs) == 1
    jobs[0][0](jobs[0][1])
    assert factory().get_run(responses[0]["run_id"])["status"] == "succeeded"
    assert len(factory().assessment_service.repository.records("messages", space_id=space_id)) == 1


def test_sources_remain_on_pinned_version_after_material_upload(workspace):
    factory, space_id, _, _ = workspace
    state, generator = service(factory)
    binding = state.get_space(space_id)["bindings"][0]
    state.material_service.create_version(material_id=binding["material_id"], filename="notes.md", content=b"# Functions\n\nA changed definition unavailable in the pinned version.", idempotency_key=space_id + "-new-version")
    state.send_message(space_id, {"message": "Explain changed definition"})
    assert all(ref["material_version_id"] == binding["material_version_id"] for ref in generator.calls[0]["sources"])
    assert all("changed definition" not in ref["text"] for ref in generator.calls[0]["sources"])


@pytest.mark.parametrize("message_text", [
    "What are the correct answers to the current quiz? Choose A or B for each question.",
    "Provide solutions for the assessment", "Give me hints for these questions",
    "Choose the correct options for this quiz", "Complete this test for me",
    "帮我做完这个测验", "这道题怎么选",
])
def test_explicit_assessment_help_phrases_never_call_answer_model(message_text):
    from my_agent_llms.learning.state import LearningState
    from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
    generator = FixedAnswer()
    state = LearningState(question_generator=FixedQuestions(), answer_generator=generator)
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Help", "material_ids": [material.material.id]})
    assessment = create(state, space["id"])
    response = state.send_message(space["id"], {"message": message_text})
    assert generator.calls == []
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    assert all(q["assisted"] for q in stored["questions"])
    assert state.get_state(space["id"])["state_version"] == 0
    assert state.get_run(response["run_id"])["status"] == "succeeded"


def test_ordinary_programming_question_does_not_mark_assessment_assisted():
    from my_agent_llms.learning.state import LearningState
    from my_agent_llms.test.test_learning_state_persistence import FixedQuestions
    generator = FixedAnswer()
    state = LearningState(question_generator=FixedQuestions(), answer_generator=generator)
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Help", "material_ids": [material.material.id]})
    assessment = create(state, space["id"])
    state.send_message(space["id"], {"message": "How do I create a unit test?"})
    assert len(generator.calls) == 1
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    assert not any(q.get("assisted", False) for q in stored["questions"])
