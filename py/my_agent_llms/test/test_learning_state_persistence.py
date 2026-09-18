"""Assessment commands exercise restart, conflict and rollback boundaries."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4
import os

import pytest
from sqlalchemy import create_engine, select, delete
from sqlalchemy.pool import StaticPool

from my_agent_llms.learning.db import init_db, AttemptRow, EvidenceRow, AssessmentRow, LearnerStateRow, StateResetRow, LearningSpaceRow, IdempotencyRow, SourceChunkRow, MaterialVersionRow, MaterialRow, RunRow
from my_agent_llms.learning.errors import DomainConflict
from my_agent_llms.learning.materials import InMemoryMaterialRepository
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository
from my_agent_llms.learning.state import LearningState


class FixedQuestions:
    """Explicit test fixture: production never defaults to these questions."""
    def generate(self, topics, payload):
        topic = topics[0]
        return [{"type": "single_choice", "prompt": f"Functions question {i}",
                 "options": [{"id": "A", "text": "Reusable behavior"}, {"id": "B", "text": "Unrelated"}],
                 "answer_key": "A", "rubric": "Select the definition", "topic_id": topic["id"],
                 "source_refs": topic["source_refs"], "difficulty": "easy", "is_application": True}
                for i in range(payload["question_count"])]


@pytest.fixture(params=["memory", "sql", "mysql"])
def workspace(request, tmp_path):
    engine = None
    label = "assessment-test-" + uuid4().hex
    if request.param == "mysql":
        url = os.getenv("LEARNING_TEST_MYSQL_URL")
        if not url:
            pytest.skip("requires explicit MySQL test URL")
        engine = create_engine(url, pool_pre_ping=True)
        assert engine.dialect.name == "mysql"
    created_run_ids = set()
    memory = InMemoryMaterialRepository()
    if request.param == "sql":
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learning.db'}")
        init_db(engine)
    def factory(generator=None):
        repo = SqlAlchemyMaterialRepository(engine) if engine else memory
        state = LearningState(repo, question_generator=generator or FixedQuestions())
        make_run = state.run_service.create
        def tracked_run(*args, **kwargs):
            run = make_run(*args, **kwargs)
            created_run_ids.add(run["id"])
            return run
        state.run_service.create = tracked_run
        execute = state.assessment_service._execute
        state.assessment_service._execute = lambda op, ident, body, key, change: execute(op, ident, body, label + key if key else None, change)
        return state
    first = factory()
    material = first.material_service.create(filename="notes.md", content=b"# Functions\n\nFunctions group reusable behavior.", idempotency_key=label + "material")
    space = first.create_space({"name": "Python", "material_ids": [material.material.id]})
    topic_id = first.topics_for_space(space["id"])[0]["id"]
    first.set_scope(space["id"], {"topic_ids": [topic_id], "expected_version": 1})
    try:
        yield factory, space["id"], topic_id, engine
    finally:
        if engine and engine.dialect.name == "mysql":
            with engine.begin() as connection:
                assessments = connection.execute(select(AssessmentRow.id, AssessmentRow.run_id, AssessmentRow.finalize_run_id).where(AssessmentRow.space_id == space["id"])).all()
                assessment_ids = [a.id for a in assessments]
                run_ids = [rid for a in assessments for rid in (a.run_id, a.finalize_run_id) if rid]
                for cls in (EvidenceRow, LearnerStateRow, StateResetRow):
                    connection.execute(delete(cls).where(cls.space_id == space["id"]))
                connection.execute(delete(AttemptRow).where(AttemptRow.assessment_id.in_(assessment_ids)))
                connection.execute(delete(AssessmentRow).where(AssessmentRow.space_id == space["id"]))
                connection.execute(delete(LearningSpaceRow).where(LearningSpaceRow.id == space["id"]))
                connection.execute(delete(IdempotencyRow).where(IdempotencyRow.key.startswith(label)))
                versions = select(MaterialVersionRow.id).where(MaterialVersionRow.material_id == material.material.id)
                connection.execute(delete(SourceChunkRow).where(SourceChunkRow.material_version_id.in_(versions)))
                connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.material_id == material.material.id))
                connection.execute(delete(MaterialRow).where(MaterialRow.id == material.material.id))
                connection.execute(delete(RunRow).where(RunRow.id.in_(set(run_ids) | created_run_ids)))
        if engine:
            engine.dispose()


def create(state, space_id, key="assessment"):
    response = state.create_assessment(space_id, {"kind": "diagnostic", "question_count": 5,
        "question_types": ["single_choice"], "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}, idempotency_key=key)
    return state.get_assessment(response["id"])


def answer(state, assessment, key="answer", finalize=False):
    return state.record_attempt(assessment["id"], {"answers": [
        {"question_id": q["id"], "expected_answer_revision": 0, "answer": "A"}
        for q in assessment["questions"]], "finalize": finalize}, idempotency_key=key)


def test_restart_restores_attempts_results_evidence_and_run(workspace):
    factory, space_id, topic_id, engine = workspace
    first = factory()
    assessment = create(first, space_id)
    assert assessment["status"] == "ready"
    UUID(assessment["id"])
    for question in assessment["questions"]:
        assert not {"answer_key", "source_refs", "rubric"} & question.keys()
    answer(first, assessment)
    second = factory()
    result = second.finalize_assessment(assessment["id"], {"allow_unanswered": False})
    third = factory()
    assert third.finalize_assessment(assessment["id"], {}) == result
    assert third.get_run(result["run_id"])["status"] == "succeeded"
    assert third.get_assessment(assessment["id"])["status"] == "completed"
    assert third.get_state(space_id)["state_version"] == 1
    items = third.get_state(space_id)["items"]
    assert items[0]["mastery_score"] == 1.0
    assert items[0]["status"] == "learning"  # One assessment never proves mastery.
    assert items[0]["independent_evidence_count"] == 5
    evidence = third.evidence_for(space_id)
    assert len(evidence) == 5
    assert all(e["attempt_id"] and e["source_refs"] for e in evidence)
    graded = third.assessment_result(assessment["id"])
    assert len(graded["topic_results"][0]["evidence_ids"]) == 5
    assert all(q["rubric_version"] and q["source_refs"] for q in graded["question_results"])
    if engine:
        with engine.connect() as connection:
            assert len(connection.execute(select(AttemptRow).where(AttemptRow.assessment_id == assessment["id"])).all()) == 5


def test_replay_and_revision_conflict_are_durable_and_batch_atomic(workspace):
    factory, space_id, _, engine = workspace
    state = factory()
    assessment = create(state, space_id)
    assert create(factory(), space_id) == assessment
    response = answer(state, assessment)
    assert answer(factory(), assessment) == response
    with pytest.raises(DomainConflict, match="版本"):
        answer(factory(), assessment, key="different-key")
    q1, q2 = assessment["questions"][:2]
    with pytest.raises(DomainConflict):
        factory().record_attempt(assessment["id"], {"answers": [
            {"question_id": q1["id"], "expected_answer_revision": 1, "answer": "B"},
            {"question_id": q2["id"], "expected_answer_revision": 0, "answer": "B"}]}, idempotency_key="batch-conflict")
    factory().finalize_assessment(assessment["id"], {})
    assert all(q["score"] == 1 for q in factory().assessment_result(assessment["id"])["question_results"])


def test_unanswered_questions_do_not_create_zero_mastery(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    with pytest.raises(DomainConflict) as exc:
        state.finalize_assessment(assessment["id"], {})
    assert exc.value.code == "ASSESSMENT_INCOMPLETE"
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    result = factory().assessment_result(assessment["id"])
    assert all(q["score"] is None for q in result["question_results"])
    assert result["topic_results"][0]["unverified_count"] == 5
    assert factory().get_state(space_id)["items"][0]["mastery_score"] is None


def test_atomic_attempt_finalize_and_idempotent_replay(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    result = answer(state, assessment, finalize=True)
    assert "run_id" in result
    assert answer(factory(), assessment, finalize=True) == result
    assert len(factory().evidence_for(space_id)) == 5
    with pytest.raises(DomainConflict):
        answer(factory(), assessment, key="late-answer")


def test_reset_keeps_history_but_old_assessment_cannot_restore_score(workspace):
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    old = create(state, space_id, key="old-open")
    answer(state, old, key="old-answers")
    state.finalize_assessment(assessment["id"], {})
    reset = state.reset_state(space_id, [topic_id], "start again", expected_state_version=1, idempotency_key="reset")
    assert reset["state_version"] == 2
    assert factory().reset_state(space_id, [topic_id], "start again", expected_state_version=1, idempotency_key="reset") == reset
    factory().finalize_assessment(old["id"], {})
    assert factory().get_state(space_id)["items"][0]["mastery_score"] is None
    assert len(factory().evidence_for(space_id)) == 10
    assert any(c["kind"] == "state_reset" for c in factory().changes_for(space_id))


def test_concurrent_finalize_publishes_once(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    clients = [factory(), factory()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda s: s.finalize_assessment(assessment["id"], {}), clients))
    assert responses[0] == responses[1]
    assert len(factory().evidence_for(space_id)) == 5
    assert factory().get_state(space_id)["state_version"] == 1


def test_finalize_failure_rolls_back_all_state_and_run_writes(workspace, monkeypatch):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("publish failed")
        patch.setattr(state.run_service, "complete", fail)
        with pytest.raises(RuntimeError):
            state.finalize_assessment(assessment["id"], {})
    assert factory().get_state(space_id)["state_version"] == 0
    assert factory().evidence_for(space_id) == []
    assert factory().get_assessment(assessment["id"])["status"] == "in_progress"
    state.finalize_assessment(assessment["id"], {})
    assert len(factory().evidence_for(space_id)) == 5


def test_hint_before_or_after_answer_excludes_independent_evidence(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    state.assessment_service.mark_assisted(space_id, assessment["questions"][0]["id"])
    answer(state, assessment)
    state.assessment_service.mark_assisted(space_id, assessment["questions"][1]["id"])
    state.finalize_assessment(assessment["id"], {})
    items = factory().get_state(space_id)["items"]
    assert items[0]["independent_evidence_count"] == 3
    assert sum(e["assisted"] for e in factory().evidence_for(space_id)) == 2


def test_cancel_during_generation_does_not_publish_questions(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    class CancellingGenerator(FixedQuestions):
        def generate(self, topics, payload):
            assessment = state.assessment_service.repository.records("assessments", space_id=space_id)[0]
            state.cancel_run(assessment["run_id"])
            return super().generate(topics, payload)
    state.assessment_service.generator = CancellingGenerator()
    assessment = create(state, space_id)
    assert assessment["status"] == "cancelled"
    assert assessment["questions"] == []
    assert factory().get_run(assessment["run_id"])["status"] == "cancelled"


def test_deleting_space_with_evidence_is_explicit_conflict(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    create(state, space_id)
    with pytest.raises(DomainConflict, match="级联"):
        state.delete_space(space_id)
    assert factory().get_space(space_id)["id"] == space_id


def test_export_contains_durable_learning_state_after_restart(workspace):
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment, finalize=True)
    restarted = factory()
    exported = restarted.export_payload(restarted.create_export(space_id)["export_id"])
    assert exported["space"]["state"][topic_id]["mastery_score"] == 1.0
    assert exported["space"]["state_version"] == restarted.get_state(space_id)["state_version"]


@pytest.mark.parametrize("finalize", [False, True])
def test_same_key_concurrent_answer_batches_replay(workspace, monkeypatch, finalize):
    from threading import Barrier
    factory, space_id, _, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("forces MySQL overlapping transaction snapshots")
    state = factory()
    assessment = create(state, space_id)
    clients = [factory(), factory()]
    barrier = Barrier(2)
    for client in clients:
        repo = client.assessment_service.repository
        original = repo.replay
        def replay(key, fingerprint, original=original, entered=[]):
            result = original(key, fingerprint)
            if not entered:
                entered.append(True)
                barrier.wait(timeout=10)
            return result
        monkeypatch.setattr(repo, "replay", replay)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda s: answer(s, assessment, finalize=finalize), clients))
    assert responses[0] == responses[1]
    assert len(state.assessment_service.repository.records("attempts", assessment_id=assessment["id"])) == 5
    assert len(state.evidence_for(space_id)) == (5 if finalize else 0)


def test_generation_is_dispatched_only_after_commit_and_once(workspace):
    factory, space_id, _, _ = workspace
    state = factory()
    jobs = []
    def dispatch(fn, identifier):
        assert factory().get_assessment(identifier)["status"] == "generating"
        jobs.append((fn, identifier))
    payload = {"question_count": 5, "difficulty_mix": {"easy": 1.0, "medium": 0.0, "hard": 0.0}}
    response = state.create_assessment(space_id, payload, idempotency_key="deferred", dispatch=dispatch)
    assert factory().get_assessment(response["id"])["questions"] == []
    assert state.create_assessment(space_id, payload, idempotency_key="deferred", dispatch=dispatch) == response
    assert len(jobs) == 1
    fn, identifier = jobs[0]
    fn(identifier)
    assert factory().get_assessment(identifier)["status"] == "ready"


def test_due_review_preserves_score_and_historical_revision_is_stale(workspace, monkeypatch):
    from datetime import datetime, timedelta
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment, finalize=True)
    original = state.get_state(space_id)["items"][0]
    future = (datetime.fromisoformat(original["next_review_at"]) + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr("my_agent_llms.learning.assessments.now", lambda: future)
    due = factory().get_state(space_id, status="needs_review")["items"][0]
    assert due["mastery_score"] == 1.0
    assert due["independent_evidence_count"] == 5
    assert due["topic_id"] == topic_id
    bound = state.space_service.bound_topics
    def changed(space):
        topics = bound(space)
        for topic in topics:
            topic["source_refs"][0]["chunk_id"] = "new-revision"
        return topics
    monkeypatch.setattr(state.space_service, "bound_topics", changed)
    stale = state.get_state(space_id)["items"][0]
    assert stale["score_validity"] == "stale"
    assert stale["status"] == "needs_review"


def test_hint_cannot_modify_other_space_or_completed_assessment(workspace):
    from my_agent_llms.learning.errors import DomainNotFound
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    qid = assessment["questions"][0]["id"]
    with pytest.raises(DomainNotFound):
        state.assessment_service.mark_assisted("other-space", qid)
    answer(state, assessment, finalize=True)
    with pytest.raises(DomainConflict):
        state.assessment_service.mark_assisted(space_id, qid)
    assert all(not e["assisted"] for e in factory().evidence_for(space_id))


def test_mysql_generation_publication_locks_run_until_commit(workspace):
    from sqlalchemy.exc import OperationalError
    factory, space_id, _, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("requires MySQL row locks")
    state = factory()
    assessment = create(state, space_id)
    run_id = assessment["run_id"]
    with state.assessment_service.repository.transaction():
        state.run_service.get(run_id)
        with engine.begin() as connection:
            with pytest.raises(OperationalError) as conflict:
                connection.execute(select(RunRow).where(RunRow.id == run_id).with_for_update(nowait=True))
            assert conflict.value.orig.args[0] == 3572  # ER_LOCK_NOWAIT


def test_mysql_delete_guard_does_not_wait_for_assessment_writer(workspace, monkeypatch):
    from threading import Event
    factory, space_id, _, engine = workspace
    if engine is None or engine.dialect.name != "mysql":
        pytest.skip("requires MySQL row locks")
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    writer, deleter = factory(), factory()
    locked, delete_done = Event(), Event()
    repo = writer.assessment_service.repository
    original = repo.get_record
    entered = []
    def held(table, identifier):
        result = original(table, identifier)
        if table == "assessments" and not entered:
            entered.append(True)
            locked.set()
            assert delete_done.wait(5), "delete guard blocked on the assessment lock"
        return result
    monkeypatch.setattr(repo, "get_record", held)
    def delete_space():
        try:
            with pytest.raises(DomainConflict) as conflict:
                deleter.delete_space(space_id)
            assert conflict.value.code == "SPACE_HAS_LEARNING_HISTORY"
        finally:
            delete_done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        grading = pool.submit(writer.finalize_assessment, assessment["id"], {})
        assert locked.wait(5)
        deleting = pool.submit(delete_space)
        deleting.result(timeout=10)
        grading.result(timeout=10)
    assert factory().get_assessment(assessment["id"])["status"] == "completed"


def test_first_family_score_survives_same_timestamp_later_submission(workspace, monkeypatch):
    from itertools import count
    factory, space_id, _, _ = workspace
    state = factory()
    descending = count(1000000, -1)
    monkeypatch.setattr("my_agent_llms.learning.assessments.uid", lambda: str(UUID(int=next(descending))))
    monkeypatch.setattr("my_agent_llms.learning.assessments.now", lambda: "2026-09-18T00:00:00+00:00")
    first = create(state, space_id)
    state.record_attempt(first["id"], {"answers": [{"question_id": q["id"], "answer": "B", "expected_answer_revision": 0} for q in first["questions"]], "finalize": True}, idempotency_key="wrong-first")
    second = create(state, space_id, key="second")
    answer(state, second, key="later-correct", finalize=True)
    result = factory().get_state(space_id)["items"][0]
    assert result["independent_evidence_count"] == 5
    assert result["mastery_score"] == 0.0
