"""Delivered source-grounded help cannot become independent assessment evidence."""
import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.conversations.generation import MessageGenerationError
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_state_persistence import FixedAnswer, FixedQuestions, answer, create


@pytest.fixture(params=["memory", "sql"])
def help_workspace(request, tmp_path):
    engine = None
    memory = InMemoryMaterialRepository()
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'help.db').as_posix()}")
        init_db(engine)
    def factory():
        return LearningState(SqlAlchemyMaterialRepository(engine) if engine else memory,
                             question_generator=FixedQuestions(), answer_generator=FixedAnswer())
    state = factory()
    material = state.material_service.create(filename="notes.md",
        content=b"# Functions\n\nFunctions group reusable behavior.\n\n# Loops\n\nLoops repeat operations.",
        idempotency_key="help-material")
    space = state.create_space({"name": "Study", "material_ids": [material.material.id]})
    topics = state.topics_for_space(space["id"])
    functions = next(t for t in topics if t["name"] == "Functions")
    loops = next(t for t in topics if t["name"] == "Loops")
    state.set_scope(space["id"], {"topic_ids": [functions["id"]], "expected_version": 1})
    yield factory, space["id"], material, functions, loops
    if engine:
        engine.dispose()


def stored(state, assessment):
    return state.assessment_service.repository.get_record("assessments", assessment["id"])


def explain(state, space_id, **kwargs):
    return state.send_message(space_id, {"message": "Explain how functions group reusable behavior."}, **kwargs)


def test_paraphrased_help_is_audited_before_evidence_and_survives_restart(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    message = explain(state, space_id)
    row = factory().message_service.repository.records("messages", space_id=space_id)[0]
    assert row["snapshot"]["hint"] is False
    assert state.get_run(message["run_id"])["status"] == "succeeded"
    audited = stored(factory(), assessment)
    assert all(q.get("assisted") for q in audited["questions"])
    event = audited["snapshot"]["assistance_events"][0]
    assert event["message_id"] == row["id"]
    assert event["space_id"] == space_id
    assert event["delivered_at"] >= row["created_at"]
    assert event["source_refs"] and event["topic_ids"] == assessment["topic_ids"]
    assert state.get_state(space_id)["state_version"] == 0
    factory().finalize_assessment(assessment["id"], {})
    assert all(e["assisted"] and not e["eligible"] for e in factory().evidence_for(space_id))


def test_help_during_question_generation_is_remembered(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state, jobs = factory(), []
    assessment = state.create_assessment(space_id, {"question_count": 5,
        "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}},
        dispatch=lambda fn, *args: jobs.append((fn, args)))
    explain(state, space_id)
    assert stored(factory(), assessment)["snapshot"].get("consulted_sources")
    fn, args = jobs[0]
    fn(*args)
    assert all(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_same_sources_in_another_space_still_mark_assistance(help_workspace):
    factory, space_id, material, functions, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    other = state.create_space({"name": "Other", "material_ids": [material.material.id]})
    state.set_scope(other["id"], {"topic_ids": [functions["id"]], "expected_version": 1})
    explain(state, other["id"])
    audit = stored(factory(), assessment)
    assert all(q.get("assisted") for q in audit["questions"])
    assert audit["snapshot"]["assistance_events"][0]["space_id"] == other["id"]


def test_unrelated_sources_in_same_material_and_completed_evidence_stay_independent(help_workspace):
    factory, space_id, material, _, loops = help_workspace
    state = factory()
    assessment = create(state, space_id)
    other = state.create_space({"name": "Loops", "material_ids": [material.material.id]})
    state.set_scope(other["id"], {"topic_ids": [loops["id"]], "expected_version": 1})
    state.send_message(other["id"], {"message": "Explain loops."})
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    before = factory().evidence_for(space_id)
    explain(state, space_id)
    assert factory().evidence_for(space_id) == before
    assert all(e["eligible"] and not e["assisted"] for e in before)


@pytest.mark.parametrize("finish", ["fail", "cancel", "stale", "delete"])
def test_undelivered_help_does_not_mark_related_assessment(help_workspace, finish):
    factory, space_id, material, functions, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    other = state.create_space({"name": "Other", "material_ids": [material.material.id]})
    state.set_scope(other["id"], {"topic_ids": [functions["id"]], "expected_version": 1})
    jobs = []
    response = explain(state, other["id"], dispatch=lambda fn, *args: jobs.append((fn, args)))
    def generate(snapshot):
        restored = factory()
        if finish == "fail":
            raise MessageGenerationError("MESSAGE_VALIDATION_FAILED")
        if finish == "cancel":
            restored.cancel_run(response["run_id"])
        elif finish == "stale":
            restored.set_scope(other["id"], {"topic_ids": [functions["id"]],
                "expected_version": restored.get_space(other["id"])["space_version"]})
        elif finish == "delete":
            restored.delete_space(other["id"], {"confirm": True,
                "expected_version": restored.get_space(other["id"])["space_version"]})
        return FixedAnswer().generate(snapshot)
    state.message_service.generator.generate = generate
    fn, args = jobs[0]
    fn(*args)
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])
    assert not stored(factory(), assessment)["snapshot"].get("assistance_events")


def test_cancelled_explicit_hint_does_not_mark_questions_at_submission(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state, jobs = factory(), []
    assessment = create(state, space_id)
    response = state.send_message(space_id, {"message": "Give me the answers to this assessment"},
        dispatch=lambda fn, *args: jobs.append((fn, args)))
    state.cancel_run(response["run_id"])
    fn, args = jobs[0]
    fn(*args)
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_assessment_created_while_model_runs_is_marked_at_delivery(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state, created = factory(), []
    def generate(snapshot):
        created.append(create(factory(), space_id))
        return FixedAnswer().generate(snapshot)
    state.message_service.generator.generate = generate
    explain(state, space_id)
    assert all(q.get("assisted") for q in stored(factory(), created[0])["questions"])


def test_finalization_before_answer_delivery_keeps_past_evidence_independent(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    def generate(snapshot):
        factory().finalize_assessment(assessment["id"], {})
        return FixedAnswer().generate(snapshot)
    state.message_service.generator.generate = generate
    explain(state, space_id)
    assert all(e["eligible"] and not e["assisted"] for e in factory().evidence_for(space_id))


def test_assistance_is_persisted_before_publication_and_rolls_back_with_it(help_workspace, monkeypatch):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    original = state.run_service.append_event
    observed = []
    def fail(run_id, event, data):
        if event == "message.completed":
            observed.append(all(q.get("assisted") for q in stored(state, assessment)["questions"]))
            raise RuntimeError("injected publication rollback")
        return original(run_id, event, data)
    monkeypatch.setattr(state.run_service, "append_event", fail)
    with pytest.raises(RuntimeError, match="publication rollback"):
        explain(state, space_id)
    assert observed == [True]
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_rag_original_source_spans_mark_legacy_question_refs(help_workspace):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    source = state.message_service._sources(state.get_space(space_id))[0]
    citation = {k: v for k, v in source.items() if k not in {"text", "topic_id", "topic_name"}}
    citation.update(chunk_id="new-retrieval-id", citation_schema_version=2,
        retrieval_version_id="retrieval-version", source_spans=[{"block": source["chunk_id"],
            "material_version_id": source["material_version_id"], "start": 0, "end": len(source["text"])}])
    class Pipeline:
        def answer(self, *args, **kwargs):
            return {"sources": [dict(citation, text=source["text"])], "trace": {},
                    "text": "Functions group reusable behavior.", "citations": [citation], "status": "answered"}
    state.message_service.rag_pipeline = Pipeline()
    explain(state, space_id)
    audit = stored(factory(), assessment)
    assert all(q.get("assisted") for q in audit["questions"])
    assert audit["snapshot"]["consulted_sources"][0]["chunk_id"] == source["chunk_id"]


def test_assessment_created_at_publication_boundary_retries_without_regeneration(help_workspace, monkeypatch):
    factory, space_id, _, _, _ = help_workspace
    state, created, calls = factory(), [], []
    prepare = state.message_service.source_access.prepare_delivery
    def prepare_with_race(sources, identifier):
        plan = prepare(sources, identifier)
        calls.append(plan)
        if not created:
            created.append(create(factory(), space_id))
        return plan
    monkeypatch.setattr(state.message_service.source_access, "prepare_delivery", prepare_with_race)
    provider_calls = []
    def generate(snapshot):
        provider_calls.append(snapshot)
        return FixedAnswer().generate(snapshot)
    state.message_service.generator.generate = generate
    response = explain(state, space_id)
    assert len(calls) == 2 and len(provider_calls) == 1
    assert state.get_run(response["run_id"])["status"] == "succeeded"
    assert all(q.get("assisted") for q in stored(factory(), created[0])["questions"])


@pytest.mark.parametrize("same_conversation", [True, False])
def test_help_carried_by_history_or_recall_retains_original_sources(help_workspace, same_conversation):
    factory, space_id, _, functions, loops = help_workspace
    state = factory()
    state.set_scope(space_id, {"topic_ids": [functions["id"], loops["id"]],
        "expected_version": state.get_space(space_id)["space_version"]})
    class OneTopicRetriever:
        def select(self, message, sources, *, limit):
            wanted = "Loops" if "loops" in message else "Functions"
            return [source for source in sources if source["topic_name"] == wanted]
    state.message_service.retriever = OneTopicRetriever()
    previous = explain(state, space_id)
    assessment = state.create_assessment(space_id, {"question_count": 5,
        "topic_ids": [functions["id"]], "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
    body = {"message": "Relate functions and loops using our prior explanation"}
    if same_conversation:
        body["session_id"] = previous["session_id"]
    response = state.send_message(space_id, body)
    assert state.get_run(response["run_id"])["status"] == "succeeded"
    assert all(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_transient_retry_records_only_successful_delivery_once(help_workspace):
    from datetime import datetime, timedelta, timezone
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    assessment = create(state, space_id)
    calls = []
    def generate(snapshot):
        calls.append(snapshot)
        if len(calls) == 1:
            raise MessageGenerationError("MODEL_UNAVAILABLE")
        return FixedAnswer().generate(snapshot)
    state.message_service.generator.generate = generate
    response = explain(state, space_id, durable=True, idempotency_key="retry-help")
    event = state.message_service.repository.records("outbox", event_type="message.generate")[0]
    timestamp = datetime.now(timezone.utc)
    assert ModelTaskWorker(messages=state.message_service, clock=lambda: timestamp).run_once(event["id"])
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])
    worker = ModelTaskWorker(messages=state.message_service, clock=lambda: timestamp + timedelta(seconds=15))
    assert worker.run_once(event["id"])
    assert not worker.run_once(event["id"])
    assert explain(factory(), space_id, durable=True, idempotency_key="retry-help") == response
    assert len(calls) == 2
    assert len(stored(factory(), assessment)["snapshot"]["assistance_events"]) == 1


def test_assessment_locks_precede_spaces_and_message_at_publication(help_workspace, monkeypatch):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    create(state, space_id)
    order = []
    original_publish = state.message_service._publish_answer_once
    def publish(*args, **kwargs):
        repository = state.message_service.repository
        original_record = repository.get_record
        original_space = state.space_service.repository.get
        original_version = state.material_repository.get_version
        def get_record(table, identifier, **options):
            if options.get("lock", True):
                order.append(table)
            return original_record(table, identifier, **options)
        def get_space(identifier):
            order.append("space")
            return original_space(identifier)
        def get_version(identifier):
            order.append("version")
            return original_version(identifier)
        with monkeypatch.context() as patch:
            patch.setattr(repository, "get_record", get_record)
            patch.setattr(state.space_service.repository, "get", get_space)
            patch.setattr(state.material_repository, "get_version", get_version)
            return original_publish(*args, **kwargs)
    monkeypatch.setattr(state.message_service, "_publish_answer_once", publish)
    explain(state, space_id)
    assert order.index("assessments") < order.index("space") < order.index("version") < order.index("messages")


def test_dropped_context_cannot_mark_unrelated_sources():
    from knowpath_backend.learning.conversations.context import bound_snapshot, delivery_source_refs, prompt_data
    current = {"material_id": "m", "material_version_id": "v", "chunk_id": "current", "text": "Current evidence."}
    old = {"material_id": "m", "material_version_id": "v", "chunk_id": "old"}
    snapshot = bound_snapshot({"message": "Explain current source", "sources": [current],
        "history": [{"role": "user", "content": "Old question " * 3000},
                    {"role": "assistant", "content": "Old explanation " * 3000}],
        "memory": {"summary": {}, "recall": []},
        "context_provenance": {"history": [[old]], "recall": {}, "summary": []}}, budget=1024)
    assert snapshot["history"] == []
    assert delivery_source_refs(snapshot) == [{k: v for k, v in current.items() if k != "text"}]
    assert "context_provenance" not in prompt_data(snapshot)


def test_rag_does_not_count_recall_or_summary_it_never_receives():
    from knowpath_backend.learning.conversations.context import delivery_source_refs
    def source(chunk):
        return {"material_id": "m", "material_version_id": "v", "chunk_id": chunk}
    snapshot = {"rag_mode": True, "sources": [source("current")], "context_provenance": {
        "history": [[source("history")]], "recall": {"msg": [source("recall")]}, "summary": [source("summary")]}}
    assert {ref["chunk_id"] for ref in delivery_source_refs(snapshot)} == {"current", "history"}


@pytest.mark.parametrize("read_kind", ["events", "run", "sse"])
def test_historical_message_redelivery_marks_new_active_assessment(help_workspace, read_kind):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    message = explain(state, space_id)
    assessment = create(state, space_id)
    if read_kind == "events":
        events = factory().events_for(message["run_id"])
        assert any(e["event"] == "message.completed" for e in events)
    elif read_kind == "run":
        assert factory().get_run(message["run_id"])["events"]
    else:
        app = create_app(state.material_service, question_generator=FixedQuestions(), answer_generator=FixedAnswer())
        with TestClient(app) as client:
            response = client.get(f"/api/v1/runs/{message['run_id']}/events")
        assert response.status_code == 200 and "Functions group reusable behavior" in response.text
    audit = stored(factory(), assessment)
    assert all(q.get("assisted") for q in audit["questions"])
    event = audit["snapshot"]["assistance_events"][0]
    assert event["kind"] == "message_replay" and event["delivery_id"]
    assert event["message_id"] == state.message_service.repository.records("messages", run_id=message["run_id"])[0]["id"]


@pytest.mark.parametrize("read_kind", ["metadata", "completed_cursor", "latest_cursor", "invalid_cursor"])
def test_reads_without_message_text_do_not_mark_assistance(help_workspace, read_kind):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    message = explain(state, space_id)
    events = state.run_service.events_for(message["run_id"])
    assessment = create(state, space_id)
    app = create_app(state.material_service, question_generator=FixedQuestions(), answer_generator=FixedAnswer())
    with TestClient(app) as client:
        path = f"/api/v1/runs/{message['run_id']}"
        if read_kind == "metadata":
            response = client.get(path)
            assert response.status_code == 200 and "events" not in response.json()
        else:
            cursor = (next(e["id"] for e in events if e["event"] == "message.completed") if read_kind == "completed_cursor"
                      else events[-1]["id"] if read_kind == "latest_cursor" else "999999")
            response = client.get(path + "/events", headers={"Last-Event-ID": cursor})
            assert response.status_code == (422 if read_kind == "invalid_cursor" else 200)
            assert "Functions group reusable behavior" not in response.text
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_replay_assistance_failure_prevents_returning_message_text(help_workspace, monkeypatch):
    factory, space_id, _, _, _ = help_workspace
    state = factory()
    message = explain(state, space_id)
    assessment = create(state, space_id)
    original = state.message_service.source_access.record_delivery
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("assistance audit rollback")
    monkeypatch.setattr(state.message_service.source_access, "record_delivery", fail)
    with pytest.raises(RuntimeError, match="audit rollback"):
        state.events_for(message["run_id"])
    assert not any(q.get("assisted") for q in stored(factory(), assessment)["questions"])


def test_legacy_history_recovers_original_sources_for_replay_and_future_context(help_workspace):
    from knowpath_backend.learning.conversations.context import project_memory
    factory, space_id, _, functions, loops = help_workspace
    state = factory()
    state.set_scope(space_id, {"topic_ids": [functions["id"], loops["id"]],
        "expected_version": state.get_space(space_id)["space_version"]})
    class OneTopicRetriever:
        def select(self, message, sources, *, limit):
            return [s for s in sources if s["topic_name"] == ("Loops" if "loops" in message else "Functions")]
    state.message_service.retriever = OneTopicRetriever()
    first = explain(state, space_id)
    second = state.send_message(space_id, {"message": "Relate functions and loops", "session_id": first["session_id"]})
    repository = state.message_service.repository
    for row in repository.records("messages", space_id=space_id):
        row["snapshot"].pop("delivered_source_refs", None)
        row["snapshot"].pop("context_provenance", None)
        repository.put_record("messages", row)
    assessment = state.create_assessment(space_id, {"question_count": 5, "topic_ids": [functions["id"]],
        "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}})
    state.events_for(second["run_id"])
    assert all(q.get("assisted") for q in stored(factory(), assessment)["questions"])
    context = project_memory(repository.records("messages", space_id=space_id),
        state.get_space(space_id), first["session_id"], "Explain loops")
    assert functions["source_refs"][0]["chunk_id"] in {r["chunk_id"] for r in context["context_provenance"]["history"][1]}


def test_erased_material_cannot_be_reexposed_using_retained_provenance(help_workspace):
    from knowpath_backend.learning.errors import DomainConflict
    factory, space_id, material, _, _ = help_workspace
    state = factory()
    message = explain(state, space_id)
    state.delete_material(material.material.id, {"confirm": True, "cascade": True,
        "expected_version": state.material_repository.get_material(material.material.id).version})
    with pytest.raises(DomainConflict):
        state.events_for(message["run_id"])


@pytest.mark.parametrize("with_space", [True, False])
def test_source_read_sees_assessment_created_after_candidate_scan(help_workspace, monkeypatch, with_space):
    from knowpath_backend.learning.errors import DomainConflict
    from knowpath_backend.learning.materials.source_access import SourceAccessService
    factory, space_id, _, functions, _ = help_workspace
    state = factory()
    repository = state.assessment_service.repository
    original = repository.records
    created, fired = [], []
    def concurrent_create(table, **kwargs):
        result = original(table, **kwargs)
        if table == "assessments" and kwargs.get("lock") is False and not fired:
            fired.append(True)
            created.append(create(state, space_id))
        return result
    monkeypatch.setattr(repository, "records", concurrent_create)
    ref = functions["source_refs"][0]
    def consult():
        return SourceAccessService(state.assessment_service).consult(ref["material_id"],
            ref["material_version_id"], [ref["chunk_id"]], space_id if with_space else None)
    if with_space:
        consult()
        assert all(q.get("assisted") for q in stored(factory(), created[0])["questions"])
    else:
        with pytest.raises(DomainConflict) as failure:
            consult()
        assert failure.value.code == "SPACE_CONTEXT_REQUIRED"
