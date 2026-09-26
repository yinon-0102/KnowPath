"""Adaptive decisions are scoped, independently supported and durable."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_graph_worker import worker, event_id


class TopicQuestions:
    def __init__(self, *, short=False, only_dependent=False, salt=""):
        self.short, self.only_dependent, self.salt = short, only_dependent, salt

    def generate(self, topics, payload):
        ordered = sorted(topics, key=lambda t: t["name"])
        if self.only_dependent:
            ordered = [t for t in ordered if t["name"] == "Functions"]
        result = []
        for index in range(payload["question_count"]):
            topic = ordered[index % len(ordered)]
            result.append({"type": "short_answer" if self.short else "single_choice",
                "prompt": f"{topic['name']} item {index} {self.salt}",
                "options": [] if self.short else [{"id": "A", "text": "Grounded definition"}, {"id": "B", "text": "Unrelated definition"}],
                "answer_key": "A", "rubric": "Use the frozen definition",
                "topic_id": topic["id"], "source_refs": topic["source_refs"],
                "difficulty": "medium", "is_application": True})
        return result


@pytest.fixture(params=["memory", "sqlite"])
def adaptive_workspace(request, tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'adaptive.db'}") if request.param == "sqlite" else None
    if engine:
        init_db(engine)
    memory = InMemoryMaterialRepository()
    def factory(generator=None):
        return LearningState(SqlAlchemyMaterialRepository(engine) if engine else memory,
                             question_generator=generator or TopicQuestions())
    state = factory()
    upload = state.material_service.create(filename="adaptive.md",
        content=b"# Basics\n\nNames bind values.\n\n# Functions\n\nPrerequisites: Basics\nFunctions reuse behavior.", idempotency_key="upload")
    staged = state.graph_service.reconcile(upload.material.id,
        {"version_id": upload.version.id, "expected_graph_version": 0}, "reconcile")
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged["candidate_revision_id"],
        {"expected_graph_version": 0, "resolutions": []}, "publish")
    space = state.create_space({"material_ids": [upload.material.id]})
    topics = {t["name"]: t["id"] for t in state.space_service.bound_topics(space) if t["id"] == t["canonical_topic_id"]}
    state.set_scope(space["id"], {"topic_ids": list(topics.values()), "expected_version": 1})
    # A real previous assessment provides strong Basics evidence, so Functions
    # is the least evidenced first diagnostic target.
    old = state.create_assessment(space["id"], {"question_count": 5, "topic_ids": [topics["Basics"]]}, idempotency_key="baseline")
    ready = state.get_assessment(old["id"])
    state.record_attempt(old["id"], {"answers": [{"question_id": q["id"], "expected_answer_revision": 0, "answer": "A"} for q in ready["questions"]], "finalize": True}, idempotency_key="baseline-answer")
    yield factory, space["id"], topics
    if engine:
        engine.dispose()


def create(state, space_id, **extra):
    response = state.create_assessment(space_id, {"question_count": 5, "adaptive": True, **extra}, idempotency_key=str(uuid4()))
    return state.get_assessment(response["id"])


def submit(state, assessment_id, question_id, answer="B", key=None, **extra):
    return state.record_attempt(assessment_id, {"answers": [{"question_id": question_id, "expected_answer_revision": 0, "answer": answer}], **extra}, idempotency_key=key or str(uuid4()))


def test_opt_in_hides_future_pool_and_freezes_weak_selection(adaptive_workspace):
    factory, space_id, topics = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    assert len(assessment["questions"]) == 1
    assert assessment["questions"][0]["topic_ids"] == [topics["Functions"]]
    diagnostic = state.assessment_service.diagnostic(assessment["id"])
    assert diagnostic["policy_version"] == "adaptive-v1"
    assert diagnostic["progress"] == {"answered": 0, "presented": 1, "target": 5}
    assert diagnostic["current_question"]["id"] == assessment["questions"][0]["id"]
    assert diagnostic["decisions"][0]["reason"] == "insufficient_evidence"
    frozen = state.assessment_service.repository.get_record("assessments", assessment["id"])["snapshot"]["adaptive"]
    assert frozen["selection_inputs"][topics["Basics"]]["evidence_ids"]
    assert frozen["historical_family_ids"]
    assert not any(secret in str(diagnostic) for secret in ("answer_key", "rubric", "source_text", "source_refs"))
    assert factory().assessment_service.diagnostic(assessment["id"]) == diagnostic


def test_frozen_legacy_mastery_uses_actual_answer_times_not_delayed_grading(adaptive_workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    factory, space_id, topics = adaptive_workspace
    topic_id = topics["Functions"]
    for index, (answered, graded) in enumerate((
        ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        ("2026-09-01T01:00:00+00:00", "2026-09-03T00:00:00+00:00"),
    )):
        state = factory(TopicQuestions(salt=f"legacy-{index}"))
        monkeypatch.setattr(assessment_module, "now", lambda: answered)
        old = state.create_assessment(space_id, {"question_count": 5, "topic_ids": [topic_id]})
        questions = state.get_assessment(old["id"])["questions"]
        state.record_attempt(old["id"], {"answers": [{"question_id": q["id"], "expected_answer_revision": 0, "answer": "A"} for q in questions]})
        monkeypatch.setattr(assessment_module, "now", lambda: graded)
        state.finalize_assessment(old["id"], {})
    repo = state.assessment_service.repository
    for row in repo.records("evidence", space_id=space_id, topic_id=topic_id):
        row.pop("observed_at", None); row.pop("observed_at_source", None)
        repo.put_record("evidence", row)
    before = repo.records("evidence", space_id=space_id, topic_id=topic_id)
    monkeypatch.setattr(assessment_module, "now", lambda: "2026-09-04T00:00:00+00:00")
    new = create(factory(TopicQuestions(salt="fresh")), space_id, topic_ids=[topic_id])
    frozen = repo.get_record("assessments", new["id"])["snapshot"]["adaptive"]["selection_inputs"][topic_id]
    assert frozen["independent_evidence_count"] == 10
    assert frozen["status"] == "learning"
    assert repo.records("evidence", space_id=space_id, topic_id=topic_id) == before


@pytest.mark.parametrize("answer,status", [("B", "supported"), ("A", "not_supported")])
def test_wrong_answer_investigates_known_prerequisite_without_claiming_cause(adaptive_workspace, answer, status):
    factory, space_id, topics = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    first = assessment["questions"][0]["id"]
    response = submit(state, assessment["id"], first, key="wrong")
    trace = factory().assessment_service.diagnostic(assessment["id"])
    assert trace["current_question"]["topic_ids"] == [topics["Basics"]]
    assert trace["hypotheses"][0]["status"] == "pending"
    assert trace["hypotheses"][0]["causal_claim"] is False
    assert trace["decisions"][-1]["reason"] == "investigate_prerequisite"
    assert submit(factory(), assessment["id"], first, key="wrong") == response
    assert factory().assessment_service.diagnostic(assessment["id"]) == trace
    submit(factory(), assessment["id"], trace["current_question"]["id"], answer)
    hypothesis = factory().assessment_service.diagnostic(assessment["id"])["hypotheses"][0]
    assert hypothesis["status"] == status
    assert len(hypothesis["attempt_ids"]) == 2


def test_future_repeated_and_batched_answers_are_rejected_atomically(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    current = assessment["questions"][0]["id"]
    stored = state.assessment_service.repository.get_record("assessments", assessment["id"])
    future = next(q["id"] for q in stored["questions"] if q["id"] != current)
    with pytest.raises(DomainConflict) as error:
        submit(state, assessment["id"], future)
    assert error.value.code == "ADAPTIVE_QUESTION_ORDER"
    with pytest.raises(DomainConflict):
        state.record_attempt(assessment["id"], {"answers": [{"question_id": q, "expected_answer_revision": 0, "answer": "A"} for q in (current, future)]}, idempotency_key="batch")
    assert state.assessment_service.repository.records("attempts", assessment_id=assessment["id"]) == []
    submit(state, assessment["id"], current)
    with pytest.raises(DomainConflict):
        submit(state, assessment["id"], current)


@pytest.mark.parametrize("mode", ["trigger_assisted", "probe_assisted", "open_answer", "reset"])
def test_non_independent_answers_cannot_support_gap(adaptive_workspace, mode):
    factory, space_id, topics = adaptive_workspace
    state = factory(TopicQuestions(short=mode == "open_answer", salt="new"))
    assessment = create(state, space_id, question_types=["short_answer" if mode == "open_answer" else "single_choice"])
    current = assessment["questions"][0]["id"]
    if mode == "trigger_assisted":
        state.assessment_service.mark_assisted(space_id, current)
    submit(state, assessment["id"], current)
    diagnostic = state.assessment_service.diagnostic(assessment["id"])
    if mode in {"trigger_assisted", "open_answer"}:
        assert diagnostic["hypotheses"] == []
        return
    probe = diagnostic["current_question"]["id"]
    if mode == "probe_assisted":
        state.assessment_service.mark_assisted(space_id, probe)
    else:
        state.reset_state(space_id, [topics["Basics"]], "reset", expected_state_version=1, idempotency_key="reset")
    submit(state, assessment["id"], probe)
    assert state.assessment_service.diagnostic(assessment["id"])["hypotheses"][0]["status"] == "inconclusive"


def test_missing_prerequisite_pool_is_explicit_and_does_not_expand_scope(adaptive_workspace):
    factory, space_id, topics = adaptive_workspace
    state = factory(TopicQuestions(only_dependent=True, salt="new"))
    assessment = create(state, space_id)
    submit(state, assessment["id"], assessment["questions"][0]["id"])
    trace = state.assessment_service.diagnostic(assessment["id"])
    assert trace["hypotheses"][0]["status"] == "inconclusive"
    assert trace["hypotheses"][0]["reason"] == "no_available_prerequisite_question"
    assert trace["current_question"]["topic_ids"] == [topics["Functions"]]


def test_finalize_uses_reached_pool_and_persists_metadata(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    while True:
        current = state.assessment_service.diagnostic(assessment["id"])["current_question"]
        if current is None:
            break
        submit(state, assessment["id"], current["id"], "A")
    state.finalize_assessment(assessment["id"], {})
    evidence = factory().assessment_service.repository.records("evidence", assessment_id=assessment["id"])
    assert len(evidence) == 5
    assert all(e["difficulty"] == "medium" and e["assessment_kind"] == "diagnostic" for e in evidence)
    attempts = {a["id"]: a for a in state.assessment_service.repository.records("attempts", assessment_id=assessment["id"])}
    assert all(e["observed_at"] == attempts[e["attempt_id"]]["created_at"] and e["observed_at_source"] == "attempt" for e in evidence)
    assert all(e["observed_at"] <= e["created_at"] for e in evidence)
    assert factory().assessment_service.diagnostic(assessment["id"])["progress"]["answered"] == 5


def test_early_finalize_does_not_expose_or_grade_unreached_questions(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    assert len(state.get_assessment(assessment["id"])["questions"]) == 1
    assert len(state.assessment_result(assessment["id"])["question_results"]) == 1
    unanswered = state.assessment_service.repository.records("evidence", assessment_id=assessment["id"])[0]
    assert unanswered["observed_at"] == unanswered["created_at"]
    assert unanswered["observed_at_source"] == "finalization_unanswered"
    assert state.assessment_service.diagnostic(assessment["id"])["current_question"] is None


def test_legacy_default_keeps_all_questions_and_answer_revisions(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory()
    created = state.create_assessment(space_id, {"question_count": 5}, idempotency_key="legacy")
    assessment = state.get_assessment(created["id"])
    assert len(assessment["questions"]) == 5 and "adaptive" not in assessment
    current = assessment["questions"][0]["id"]
    submit(state, assessment["id"], current)
    state.record_attempt(assessment["id"], {"answers": [{"question_id": current, "expected_answer_revision": 1, "answer": "A"}]}, idempotency_key="revision")
    assert state.get_assessment(assessment["id"])["answers"][0]["answer_revision"] == 2


def test_concurrent_replay_and_conflicts_advance_exactly_once(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    qid = assessment["questions"][0]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: submit(s, assessment["id"], qid, key="same-key"), [factory(), factory()]))
    assert results[0] == results[1]
    trace = factory().assessment_service.diagnostic(assessment["id"])
    assert trace["progress"]["answered"] == 1 and len(trace["decisions"]) == 2
    qid = trace["current_question"]["id"]
    def competing(key):
        try:
            return submit(factory(), assessment["id"], qid, key=key)["status"]
        except DomainConflict as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing, ["first", "second"]))
    assert sorted(results) == ["ADAPTIVE_QUESTION_ORDER", "recorded"]
    assert factory().assessment_service.diagnostic(assessment["id"])["progress"]["answered"] == 2


def test_failed_decision_commit_rolls_back_answer_and_hypothesis(adaptive_workspace, monkeypatch):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    before = state.assessment_service.diagnostic(assessment["id"])
    put = state.assessment_service.repository.put_record
    def fail(table, value):
        if table == "assessments" and value["status"] == "in_progress":
            raise RuntimeError("decision commit failed")
        return put(table, value)
    with monkeypatch.context() as patch:
        patch.setattr(state.assessment_service.repository, "put_record", fail)
        with pytest.raises(RuntimeError, match="decision commit"):
            submit(state, assessment["id"], assessment["questions"][0]["id"], key="retry")
    assert factory().assessment_service.diagnostic(assessment["id"]) == before
    assert state.assessment_service.repository.records("attempts", assessment_id=assessment["id"]) == []
    submit(factory(), assessment["id"], assessment["questions"][0]["id"], key="retry")
    assert factory().assessment_service.diagnostic(assessment["id"])["progress"]["answered"] == 1


def test_scope_does_not_expand_to_prerequisite_and_repeat_pool_stops(adaptive_workspace):
    factory, space_id, topics = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id, topic_ids=[topics["Functions"]])
    submit(state, assessment["id"], assessment["questions"][0]["id"])
    trace = state.assessment_service.diagnostic(assessment["id"])
    assert trace["hypotheses"][0]["reason"] == "prerequisite_out_of_scope"
    assert all(d["topic_id"] == topics["Functions"] for d in trace["decisions"])
    repeated = create(factory(), space_id, topic_ids=[topics["Basics"]])
    trace = factory().assessment_service.diagnostic(repeated["id"])
    assert trace["current_question"] is None
    assert trace["completion_reason"] == "no_unseen_question_family"
    factory().finalize_assessment(repeated["id"], {})
    assert factory().assessment_result(repeated["id"])["question_results"] == []


def test_completed_question_review_revalidates_hypothesis(adaptive_workspace):
    from knowpath_backend.test.test_learning_question_reviews import report, decision
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    trigger = assessment["questions"][0]["id"]
    submit(state, assessment["id"], trigger)
    probe = state.assessment_service.diagnostic(assessment["id"])["current_question"]["id"]
    submit(state, assessment["id"], probe)
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    assert state.assessment_service.diagnostic(assessment["id"])["hypotheses"][0]["status"] == "supported"
    reviewed = report(state, assessment["id"], trigger)
    assert state.assessment_service.diagnostic(assessment["id"])["hypotheses"][0]["status"] == "inconclusive"
    state.assessment_service.resolve_question_review(assessment["id"], reviewed["review_id"],
        decision(state, assessment["id"], trigger), "resolve")
    hypothesis = factory().assessment_service.diagnostic(assessment["id"])["hypotheses"][0]
    assert hypothesis["status"] == "inconclusive"
    assert hypothesis["reason"] == "trigger_not_independent_incorrect"


def test_unreached_questions_cannot_be_reported_or_reviewed_after_early_finalize(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    pool = state.assessment_service.repository.get_record("assessments", assessment["id"])["questions"]
    future = next(q["id"] for q in pool if q["id"] != assessment["questions"][0]["id"])
    state.finalize_assessment(assessment["id"], {"allow_unanswered": True})
    with pytest.raises(DomainConflict) as error:
        state.assessment_service.report_question(assessment["id"], {"question_id": future, "reason": "unseen", "expected_review_version": 0}, "hidden")
    assert error.value.code == "QUESTION_NOT_PRESENTED"
    with pytest.raises(DomainConflict) as error:
        state.assessment_service.review_grade(assessment["id"], {"question_id": future, "reason": "unseen"}, "hidden-grade")
    assert error.value.code == "QUESTION_NOT_PRESENTED"


def test_question_view_is_safe_and_strict_through_diagnostic_router(adaptive_workspace):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api.routers.diagnostics import router
    from knowpath_backend.learning.api.errors import install_error_handlers
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="new"))
    assessment = create(state, space_id)
    app = FastAPI()
    app.state.learning_state = state
    app.include_router(router)
    install_error_handlers(app)
    with TestClient(app) as client:
        path = f"/api/v1/assessments/{assessment['id']}/diagnostic"
        assert client.get(path).json() == state.assessment_service.diagnostic(assessment["id"])
        assert client.get(path, params={"include_answers": "true"}).status_code == 422
        assert client.get("/api/v1/assessments/absent/diagnostic").status_code == 404


def queued_diagnostics(state, space_id):
    return [state.assessment_service.create(space_id, {"question_count": 5, "adaptive": True},
        "queued-diagnostic-" + str(index), dispatch=lambda *args: None) for index in range(2)]


def reached_families(state, assessment_id):
    row = state.assessment_service.repository.get_record("assessments", assessment_id)
    shown = set(row["snapshot"]["adaptive"]["presented_question_ids"])
    return {question["family_id"] for question in row["questions"] if question["id"] in shown}


def test_queued_concurrent_publications_never_expose_the_same_family(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="overlapping-pools"))
    queued = queued_diagnostics(state, space_id)
    before = {a["id"]: deepcopy(state.assessment_service.repository.get_record("assessments", a["id"])["snapshot"]["adaptive"])
              for a in queued}
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(state.assessment_service.generate, [a["id"] for a in queued]))
    first, second = [reached_families(factory(), a["id"]) for a in queued]
    assert first and second and first.isdisjoint(second)
    audits = []
    for assessment in queued:
        row = factory().assessment_service.repository.get_record("assessments", assessment["id"])
        frozen = row["snapshot"]["adaptive"]
        for field in ("historical_family_ids", "selection_inputs", "frozen_at"):
            assert frozen[field] == before[assessment["id"]][field]
        audits.extend(frozen.get("runtime_family_exclusions", []))
        assert len(row["questions"]) == 5
    assert audits and all(item["reason"] == "already_exposed_in_space" for item in audits)


def test_interleaved_queued_diagnostics_preserve_first_observations_and_replay(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="interleaved-pools"))
    first, second = queued_diagnostics(state, space_id)
    for row in (first, second):
        state.assessment_service.generate(row["id"])
    pools = {row["id"]: deepcopy(state.assessment_service.repository.get_record("assessments", row["id"])["questions"])
             for row in (first, second)}
    first_question = state.assessment_service.diagnostic(first["id"])["current_question"]["id"]
    result = submit(state, first["id"], first_question, "B", key="interleaved-first")
    before_replay = factory().assessment_service.diagnostic(first["id"])
    assert submit(factory(), first["id"], first_question, "B", key="interleaved-first") == result
    assert factory().assessment_service.diagnostic(first["id"]) == before_replay
    second_question = factory().assessment_service.diagnostic(second["id"])["current_question"]["id"]
    submit(factory(), second["id"], second_question, "B")
    assert reached_families(factory(), first["id"]).isdisjoint(reached_families(factory(), second["id"]))
    first_probe = factory().assessment_service.diagnostic(first["id"])["current_question"]["id"]
    submit(factory(), first["id"], first_probe, "B")
    original_hypothesis = factory().assessment_service.diagnostic(first["id"])["hypotheses"][0]
    assert original_hypothesis["status"] == "supported"
    second_probe = factory().assessment_service.diagnostic(second["id"])["current_question"]["id"]
    submit(factory(), second["id"], second_probe, "B")
    assert factory().assessment_service.diagnostic(first["id"])["hypotheses"][0] == original_hypothesis
    assert reached_families(factory(), first["id"]).isdisjoint(reached_families(factory(), second["id"]))
    assert factory().assessment_service.diagnostic(second["id"])["current_question"] is None
    for row in (first, second):
        stored = factory().assessment_service.repository.get_record("assessments", row["id"])
        assert stored["questions"] == pools[row["id"]]
        assert len(stored["snapshot"]["adaptive"].get("runtime_family_exclusions", [])) <= 5


def test_queued_exact_repeated_pool_exhausts_without_duplicate_confirmation(adaptive_workspace):
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="exhausted-queued-pool"))
    first, second = queued_diagnostics(state, space_id)
    state.assessment_service.generate(first["id"])
    while True:
        current = state.assessment_service.diagnostic(first["id"])["current_question"]
        if current is None:
            break
        submit(state, first["id"], current["id"], "B")
    state.finalize_assessment(first["id"], {})
    state.assessment_service.generate(second["id"])
    view = factory().assessment_service.diagnostic(second["id"])
    assert view["current_question"] is None
    assert view["completion_reason"] == "no_unseen_question_family"
    assert view["progress"] == {"answered": 0, "presented": 0, "target": 5}
    assert view["hypotheses"] == []
    assert view["runtime_excluded_family_count"] == 5
    assert not any(secret in str(view) for secret in ("answer_key", "source_text", "source_refs"))
    state.finalize_assessment(second["id"], {})
    assert factory().assessment_result(second["id"])["question_results"] == []


def test_runtime_history_limit_stops_before_unverified_exposure(adaptive_workspace, monkeypatch):
    from knowpath_backend.learning.assessments import adaptive
    factory, space_id, _ = adaptive_workspace
    state = factory(TopicQuestions(salt="bounded-history"))
    assessment = create(state, space_id)
    before = factory().assessment_service.diagnostic(assessment["id"])
    monkeypatch.setattr(adaptive, "MAX_RUNTIME_HISTORY", 0)
    question_id = before["current_question"]["id"]
    result = submit(state, assessment["id"], question_id, key="bounded-answer")
    after = factory().assessment_service.diagnostic(assessment["id"])
    assert after["current_question"] is None
    assert after["completion_reason"] == "exposure_history_limit"
    assert after["progress"] == {"answered": 1, "presented": 1, "target": 5}
    assert after["decisions"] == before["decisions"]
    assert submit(factory(), assessment["id"], question_id, key="bounded-answer") == result
    assert factory().assessment_service.diagnostic(assessment["id"]) == after
    state.finalize_assessment(assessment["id"], {})
    assert len(factory().assessment_result(assessment["id"])["question_results"]) == 1
