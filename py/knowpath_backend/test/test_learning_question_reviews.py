"""Human question adjudication never turns a complaint into unearned credit."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.test.test_learning_state_persistence import workspace, create, answer, FixedQuestions


def completed(factory, space_id, *, wrong=False, assisted=False):
    state = factory()
    assessment = create(state, space_id)
    qid = assessment["questions"][0]["id"]
    if assisted:
        state.assessment_service.mark_assisted(space_id, qid)
    answer(state, assessment)
    if wrong:
        state.record_attempt(assessment["id"], {"answers": [{"question_id": qid, "answer": "B", "expected_answer_revision": 1}]})
    state.finalize_assessment(assessment["id"], {})
    return state, assessment, qid


def report(state, aid, qid, version=0, key="report"):
    return state.assessment_service.report_question(aid, {"question_id": qid, "reason": "The generated key appears wrong; please give full credit",
        "expected_review_version": version}, key)


def decision(state, aid, qid, version=1, **changes):
    frozen = state.assessment_service.repository.get_record("assessments", aid)
    question = next(q for q in frozen["questions"] if q["id"] == qid)
    return {"action": "correct", "expected_review_version": version, "confirmed": True,
        "reason": "I checked the cited frozen source and confirm the correction",
        "source_refs": question["source_refs"], "corrected_answer_key": "B",
        "corrected_rubric": "Select B according to the cited source", **changes}


def selected(state, aid, qid):
    return next(q for q in state.assessment_result(aid)["question_results"] if q["question_id"] == qid)


def test_question_review_http_report_contract():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app(question_generator=FixedQuestions())
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="question-review-material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id]})
    state, assessment, qid = completed(lambda: state, space["id"])
    url = f"/api/v1/assessments/{assessment['id']}/question-reviews"
    body = {"question_id": qid, "reason": "Wrong key", "expected_review_version": 0}
    with TestClient(app) as client:
        first = client.post(url, json=body, headers={"Idempotency-Key": "http-report"})
        assert first.status_code == 201
        assert client.post(url, json=body).status_code == 400
        assert client.post(url, json=body, headers={"Idempotency-Key": "http-report"}).json() == first.json()
        assert client.post(url, json={**body, "reason": "changed complaint"}, headers={"Idempotency-Key": "http-report"}).status_code == 409
        view = client.get(url).json()
        assert view["review_version"] == 1
        assert view["items"][0]["question"]["source_refs"]
        assert view["items"][0]["source_text"]
        resolve_url = f"{url}/{first.json()['review_id']}/resolve"
        correction = decision(state, assessment["id"], qid)
        assert client.post(resolve_url, json={**correction, "confirmed": False}, headers={"Idempotency-Key": "false"}).status_code == 422
        assert client.post(resolve_url, json={**correction, "score": 1}, headers={"Idempotency-Key": "extra"}).status_code == 422
        resolved = client.post(resolve_url, json=correction, headers={"Idempotency-Key": "resolve"})
        assert resolved.status_code == 200
        assert client.post(resolve_url, json=correction, headers={"Idempotency-Key": "resolve"}).json() == resolved.json()


def test_report_quarantines_and_confirmed_correction_replays_after_restart(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessments
    import knowpath_backend.learning.assessments.question_reviews as question_reviews
    import knowpath_backend.learning.assessments.grade_reviews as grade_reviews
    factory, sid, _, _ = workspace
    first_answer_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    wrong_answer_at = first_answer_at + timedelta(hours=2)
    clock = [first_answer_at.isoformat()]
    monkeypatch.setattr(assessments, "now", lambda: clock[0])
    monkeypatch.setattr(question_reviews, "now", lambda: clock[0])
    monkeypatch.setattr(grade_reviews, "now", lambda: clock[0])
    state = factory()
    assessment = create(state, sid)
    qid = assessment["questions"][0]["id"]
    answer(state, assessment)
    clock[0] = wrong_answer_at.isoformat()
    state.record_attempt(assessment["id"], {"answers": [{"question_id": qid, "answer": "B", "expected_answer_revision": 1}]})
    clock[0] = (first_answer_at + timedelta(days=1)).isoformat()
    state.finalize_assessment(assessment["id"], {})
    aid = assessment["id"]
    repo = state.assessment_service.repository
    original = repo.get_record("assessments", aid)
    old_result = selected(state, aid, qid)
    old = repo.get_record("evidence", old_result["evidence_id"])
    before_state = state.get_state(sid)["items"][0]
    assert before_state["next_review_at"] == (wrong_answer_at + timedelta(days=1)).isoformat()
    clock[0] = (first_answer_at + timedelta(days=7)).isoformat()
    response = report(state, aid, qid)
    assert report(factory(), aid, qid) == response
    pending = selected(factory(), aid, qid)
    assert pending["score"] is None and pending["verdict"] == "unverified"
    assert pending["question_review_status"] == "pending"
    assert factory().get_state(sid)["items"][0]["independent_evidence_count"] == 4
    assert factory().assessment_result(aid)["topic_results"][0]["verified_count"] == 4
    with pytest.raises(DomainConflict) as exc:
        state.grade_review(aid, {"question_id": qid, "reason": "force score"}, idempotency_key="grade-pending")
    assert exc.value.code == "QUESTION_QUARANTINED"
    body = decision(state, aid, qid)
    clock[0] = (first_answer_at + timedelta(days=8)).isoformat()
    resolved = state.assessment_service.resolve_question_review(aid, response["review_id"], body, "resolve")
    assert factory().assessment_service.resolve_question_review(aid, response["review_id"], body, "resolve") == resolved
    assert selected(factory(), aid, qid)["score"] == 1.0
    after = factory().get_state(sid)["items"][0]
    assert after["independent_evidence_count"] == before_state["independent_evidence_count"]
    # Correcting a wrong key changes the original assessment from failure to
    # success. review-v2 now anchors at its first answer, not the later wrong
    # attempt; neither grading nor the correction date earns new spacing.
    assert after["next_review_at"] == (first_answer_at + timedelta(days=1)).isoformat()
    assert after["review_schedule"]["anchor_observed_at"] == first_answer_at.isoformat()
    assert after["review_schedule"]["stage"] == 0
    assert after["mastery_score"] == 1.0
    frozen = factory().assessment_service.repository.get_record("assessments", aid)
    assert frozen["questions"] == original["questions"]
    assert frozen["snapshot"]["original_graded_result"] == original["result"]
    events = frozen["snapshot"]["question_review_events"]
    assert len(events) == 2 and events[0]["action"] == "report"
    new = factory().assessment_service.repository.get_record("evidence", selected(factory(), aid, qid)["evidence_id"])
    for name in ("created_at", "observed_at", "observed_at_source", "family_id", "submission_id", "submission_sequence", "attempt_id", "epoch", "topic_revision_id"):
        assert new[name] == old[name]
    assert new["observed_at"] == wrong_answer_at.isoformat()
    assert new["reviewed_at"] == clock[0]
    assert after["next_review_at"] != (datetime.fromisoformat(new["reviewed_at"]) + timedelta(days=1)).isoformat()
    assert new["observation_id"] == old["id"]
    assert new["rubric_version"] != old["rubric_version"]
    clock[0] = (first_answer_at + timedelta(days=9)).isoformat()
    state.grade_review(aid, {"question_id": qid, "reason": "recompute confirmed key"}, idempotency_key="grade-corrected")
    assert selected(factory(), aid, qid)["score"] == 1.0
    replayed_state = factory().get_state(sid)["items"][0]
    assert replayed_state["next_review_at"] == after["next_review_at"]
    assert replayed_state["review_schedule"]["anchor_observed_at"] == after["review_schedule"]["anchor_observed_at"]
    assert replayed_state["review_schedule"]["stage"] == after["review_schedule"]["stage"]


@pytest.mark.parametrize("action", ["invalidate", "reject"])
def test_invalidate_or_reject_requires_confirmation_and_preserves_audit(workspace, action):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    pending = report(state, aid, qid)
    body = decision(state, aid, qid, action=action, corrected_answer_key=None, corrected_rubric=None)
    state.assessment_service.resolve_question_review(aid, pending["review_id"], body, "resolve")
    current = selected(factory(), aid, qid)
    assert current["score"] == (1.0 if action == "reject" else None)
    if action == "invalidate":
        with pytest.raises(DomainConflict, match="隔离"):
            state.grade_review(aid, {"question_id": qid, "reason": "restore"}, idempotency_key="grade-invalid")
    with pytest.raises(DomainConflict) as exc:
        state.assessment_service.resolve_question_review(aid, pending["review_id"], {**body, "expected_review_version": 2}, "again")
    assert exc.value.code == "QUESTION_REVIEW_RESOLVED"


@pytest.mark.parametrize("changes,code", [({"source_refs": []}, "validation"), ({"source_refs": [{"chunk_id": "not-frozen"}]}, "INVALID_SOURCE_PROOF"),
    ({"corrected_answer_key": "Z"}, "INVALID_CORRECTION"), ({"corrected_answer_key": None}, "INVALID_CORRECTION"),
    ({"confirmed": False}, "validation"), ({"corrected_rubric": None}, "validation"), ({"expected_review_version": 0}, "VERSION_CONFLICT")])
def test_invalid_decisions_leave_question_quarantined(workspace, changes, code):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    pending = report(state, aid, qid)
    before = state.assessment_result(aid)
    with pytest.raises(ValidationError if code == "validation" else DomainConflict) as exc:
        state.assessment_service.resolve_question_review(aid, pending["review_id"], decision(state, aid, qid, **changes), "bad")
    if code != "validation":
        assert exc.value.code == code
    assert factory().assessment_result(aid) == before


@pytest.mark.parametrize("ineligible", ["assisted", "reset", "stale"])
def test_confirmed_correction_keeps_original_eligibility_limits(workspace, ineligible):
    factory, sid, topic, _ = workspace
    state, assessment, qid = completed(factory, sid, assisted=ineligible == "assisted", wrong=True)
    aid = assessment["id"]
    pending = report(state, aid, qid)
    if ineligible == "reset":
        state.reset_state(sid, [topic], "reset", expected_state_version=2)
    if ineligible == "stale":
        # Simulate an actual changed topic revision at the domain boundary.
        original_topics = state.assessment_service._topics
        state.assessment_service._topics = lambda s: [dict(t, revision_id="different-revision") for t in original_topics(s)]
    state.assessment_service.resolve_question_review(aid, pending["review_id"], decision(state, aid, qid), "resolve")
    new = state.assessment_service.repository.get_record("evidence", selected(state, aid, qid)["evidence_id"])
    assert not new["eligible"]
    assert new["assisted"] == (ineligible == "assisted")


def test_repeated_dispute_restores_last_confirmed_definition_only(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid, wrong=True)
    aid = assessment["id"]
    first = report(state, aid, qid)
    state.assessment_service.resolve_question_review(aid, first["review_id"], decision(state, aid, qid), "resolve-first")
    audit = deepcopy(state.assessment_service.repository.get_record("assessments", aid)["snapshot"]["question_review_events"])
    second = report(state, aid, qid, 2, "second")
    body = decision(state, aid, qid, 3, action="reject", corrected_answer_key=None, corrected_rubric=None)
    state.assessment_service.resolve_question_review(aid, second["review_id"], body, "reject-second")
    assert selected(factory(), aid, qid)["score"] == 1.0
    assert state.assessment_service.repository.get_record("assessments", aid)["snapshot"]["question_review_events"][:2] == audit


def test_question_scope_and_pending_version_conflicts(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    with pytest.raises(DomainNotFound):
        report(state, aid, "other-question")
    with pytest.raises(DomainNotFound):
        state.assessment_service.resolve_question_review(aid, "missing-review", decision(state, aid, qid, 0), "missing")
    first = report(state, aid, qid)
    with pytest.raises(DomainConflict) as exc:
        report(state, aid, qid, 1, "other")
    assert exc.value.code == "QUESTION_REVIEW_PENDING"
    with pytest.raises(DomainConflict) as exc:
        report(state, aid, assessment["questions"][1]["id"], 0, "stale-version")
    assert exc.value.code == "VERSION_CONFLICT"
    assert selected(state, aid, assessment["questions"][1]["id"])["score"] == 1.0


@pytest.mark.parametrize("stage", ["report", "resolve"])
def test_publication_failure_rolls_back_every_review_effect(workspace, monkeypatch, stage):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    pending = report(state, aid, qid) if stage == "resolve" else None
    repo = state.assessment_service.repository
    before = {t: repo.records(t, **({"assessment_id": aid} if t in {"evidence", "attempts"} else {"space_id": sid})) for t in ("assessments", "states", "evidence", "attempts")}
    version = state.get_state(sid)["state_version"]
    def fail(*args, **kwargs):
        raise RuntimeError("injected publication failure")
    monkeypatch.setattr(state.run_service, "complete", fail)
    with pytest.raises(RuntimeError):
        if stage == "report":
            report(state, aid, qid)
        else:
            state.assessment_service.resolve_question_review(aid, pending["review_id"], decision(state, aid, qid), "resolve")
    for table, records in before.items():
        assert factory().assessment_service.repository.records(table, **({"assessment_id": aid} if table in {"evidence", "attempts"} else {"space_id": sid})) == records
    assert factory().get_state(sid)["state_version"] == version


def test_concurrent_report_replays_once_and_resolution_conflicts(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda s: report(s, aid, qid), [factory(), factory()]))
    assert outcomes[0] == outcomes[1]
    body = decision(state, aid, qid)
    def resolve(key):
        try:
            return factory().assessment_service.resolve_question_review(aid, outcomes[0]["review_id"], body, key)
        except DomainConflict as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolutions = list(pool.map(resolve, ["one", "two"]))
    assert sum(isinstance(r, dict) for r in resolutions) == 1
    assert "VERSION_CONFLICT" in resolutions
    assert len(factory().assessment_service.repository.records("evidence", assessment_id=aid)) == 7


def test_review_source_disclosure_marks_overlapping_active_assessments_assisted(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    report(state, assessment["id"], qid)
    active = create(state, sid, "second-assessment")
    assert not any(q.get("assisted") for q in state.assessment_service.repository.get_record("assessments", active["id"])["questions"])
    view = state.assessment_service.question_reviews(assessment["id"])
    assert view["items"][0]["source_text"]
    record = factory().assessment_service.repository.get_record("assessments", active["id"])
    assert all(q.get("assisted") for q in record["questions"])
    assert any(event["kind"] == "question_review" for event in record["snapshot"]["assistance_events"])


def test_short_answer_rubric_correction_does_not_invent_reliable_grading(workspace):
    factory, sid, _, _ = workspace
    class ShortQuestions(FixedQuestions):
        def generate(self, topics, payload):
            return [dict(q, type="short_answer", options=[], answer_key=None) for q in super().generate(topics, payload)]
    state = factory(ShortQuestions())
    assessment = state.create_assessment(sid, {"question_count": 5, "question_types": ["short_answer"]})
    assessment = state.get_assessment(assessment["id"])
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    qid, aid = assessment["questions"][0]["id"], assessment["id"]
    pending = report(state, aid, qid)
    body = decision(state, aid, qid, corrected_answer_key=None, corrected_rubric="Explain reusable behavior")
    state.assessment_service.resolve_question_review(aid, pending["review_id"], body, "resolve")
    assert selected(factory(), aid, qid)["score"] is None
    assert factory().get_state(sid)["items"][0]["mastery_score"] is None


def test_review_changes_only_named_snapshot_and_missing_source_blocks_confirmation(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    other = create(state, sid, "second-assessment")
    answer(state, other, "answer-second")
    state.finalize_assessment(other["id"], {})
    unchanged = state.assessment_result(other["id"])
    pending = report(state, assessment["id"], qid)
    state.assessment_service.resolve_question_review(assessment["id"], pending["review_id"], decision(state, assessment["id"], qid), "resolve")
    assert factory().assessment_result(other["id"]) == unchanged
    own = state.assessment_service.repository.get_record("assessments", other["id"])
    own["snapshot"]["topics"][0]["source_text"] = ""
    state.assessment_service.repository.put_record("assessments", own)
    other_qid = other["questions"][0]["id"]
    pending = report(state, other["id"], other_qid, key="report-other")
    with pytest.raises(DomainConflict) as exc:
        state.assessment_service.resolve_question_review(other["id"], pending["review_id"], decision(state, other["id"], other_qid), "resolve-missing-source")
    assert exc.value.code == "INVALID_SOURCE_PROOF"


def test_rejecting_new_dispute_does_not_revive_invalidated_question(workspace):
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    aid = assessment["id"]
    first = report(state, aid, qid)
    invalid = decision(state, aid, qid, action="invalidate", corrected_answer_key=None, corrected_rubric=None)
    state.assessment_service.resolve_question_review(aid, first["review_id"], invalid, "invalidate")
    second = report(state, aid, qid, 2, "report-again")
    rejection = {**invalid, "action": "reject", "expected_review_version": 3}
    state.assessment_service.resolve_question_review(aid, second["review_id"], rejection, "reject-again")
    assert selected(factory(), aid, qid)["question_review_status"] == "invalid"
    assert selected(factory(), aid, qid)["score"] is None


def test_review_read_failure_rolls_back_assistance_and_handles_new_candidate(workspace, monkeypatch):
    from knowpath_backend.learning.assessments import question_reviews as reviews
    from knowpath_backend.learning.materials.source_access import SourceAccessService
    factory, sid, _, _ = workspace
    state, assessment, qid = completed(factory, sid)
    report(state, assessment["id"], qid)
    original_prepare = SourceAccessService.prepare_delivery
    new_assessments = []
    def create_at_boundary(self, sources, space_id):
        plan = original_prepare(self, sources, space_id)
        if not new_assessments:
            new_assessments.append(create(factory(), sid, "new-assessment-during-read"))
        return plan
    monkeypatch.setattr(SourceAccessService, "prepare_delivery", create_at_boundary)
    state.assessment_service.question_reviews(assessment["id"])
    active = factory().assessment_service.repository.get_record("assessments", new_assessments[0]["id"])
    assert all(q.get("assisted") for q in active["questions"])
    assert len(active["snapshot"]["assistance_events"]) == 1
    def fail(*args):
        raise RuntimeError("read response serialization failed")
    monkeypatch.setattr(reviews, "_review_view", fail)
    with pytest.raises(RuntimeError):
        state.assessment_service.question_reviews(assessment["id"])
    assert factory().assessment_service.repository.get_record("assessments", active["id"]) == active


def test_question_review_requires_completed_assessment(workspace):
    factory, sid, _, _ = workspace
    state = factory()
    assessment = create(state, sid)
    with pytest.raises(DomainConflict) as exc:
        report(state, assessment["id"], assessment["questions"][0]["id"])
    assert exc.value.code == "ASSESSMENT_NOT_READY"
    with pytest.raises(DomainConflict) as exc:
        state.assessment_service.question_reviews(assessment["id"])
    assert exc.value.code == "ASSESSMENT_NOT_READY"
