"""Shared review rules use actual observations, never correction clocks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from knowpath_backend.test.test_learning_state_persistence import workspace, create, answer

START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def evidence(i, *, days=0, score=1.0, **extra):
    return {"id": str(i), "topic_id": "t", "topic_revision_id": "r", "epoch": 0,
            "family_id": str(i), "assessment_id": str(i), "submission_sequence": i,
            "eligible": True, "assisted": False, "kind": "objective_answer",
            "score": score, "result": "correct" if score == 1 else "incorrect",
            "difficulty": "hard", "is_application": True, "error_tags": [],
            "created_at": (START + timedelta(days=days)).isoformat(), **extra}


def schedule(rows, day=30, **kwargs):
    from knowpath_backend.learning.assessments.review_policy import review_schedule
    return review_schedule(rows, revision_id="r", epoch=0, as_of=START + timedelta(days=day), **kwargs)


def test_spaced_success_advances_bounded_review_and_failure_shortens():
    rows = [evidence(i, days=i * 2) for i in range(4)]
    assert [schedule(rows[:i])["interval_days"] for i in range(1, 5)] == [1, 3, 7, 14]
    failed = schedule([*rows, evidence(4, days=8, score=0)])
    assert failed["interval_days"] == 1
    assert failed["next_review_at"] == (START + timedelta(days=9)).isoformat()
    assert "independent_failure" in failed["reasons"]
    assert failed["policy_version"] == "review-v2"


def test_unspaced_or_assisted_or_duplicate_success_cannot_extend_clock():
    first = evidence(0)
    base = schedule([first])
    for extra in [evidence(1, days=0.5), evidence(2, days=8, assisted=True),
                  evidence(3, days=8, family_id=first["family_id"]),
                  evidence(4, days=8, assessment_id=first["assessment_id"])]:
        assert schedule([first, extra])["next_review_at"] == base["next_review_at"]


def test_easy_and_unknown_difficulty_are_conservative_and_explained():
    easy = [evidence(i, days=i * 2, difficulty="easy") for i in range(5)]
    legacy = [{k: v for k, v in row.items() if k != "difficulty"} for row in easy]
    assert schedule(easy)["interval_days"] == 3
    result = schedule(legacy)
    assert result["interval_days"] == 3
    assert result["evidence_sufficiency"]["unknown_difficulty_count"] == 5
    assert "unknown_difficulty_conservative_cap" in result["reasons"]
    assert schedule([])["next_review_at"] is None
    assert schedule([])["evidence_sufficiency"]["status"] == "unknown"


def test_regrade_is_effective_only_at_audit_time_and_retains_observation_clock():
    old = evidence(0, score=0)
    revised_at = (START + timedelta(days=10)).isoformat()
    replacement = {**old, "id": "replacement", "score": 1, "result": "correct",
                   "observation_id": old["id"], "replaces_evidence_id": old["id"],
                   "reviewed_at": revised_at, "review_id": "review"}
    revoked = {**old, "eligible": False, "eligibility_before_revocation": True,
               "revoked_at": revised_at, "revoked_by_review_id": "review"}
    assert schedule([revoked, replacement], day=5)["selected_evidence_ids"] == [old["id"]]
    current = schedule([revoked, replacement])
    assert current["selected_evidence_ids"] == ["replacement"]
    assert current["next_review_at"] == schedule([old])["next_review_at"]
    assert current["last_observed_at"] == old["created_at"]


def test_filters_epoch_revision_revocation_and_future_without_mutating_inputs():
    rows = [evidence(0, epoch=2), evidence(1, topic_revision_id="old"),
            evidence(2, revoked_by_review_id="bad"), evidence(3, days=99)]
    original = deepcopy(rows)
    assert schedule(rows)["selected_evidence_ids"] == []
    assert rows == original


def test_delayed_grading_is_availability_not_observation_spacing():
    rows = [evidence(0, days=10, observed_at=START.isoformat()),
            evidence(1, days=20, observed_at=(START + timedelta(hours=12)).isoformat())]
    assert schedule(rows, day=5)["selected_evidence_ids"] == []
    assert schedule(rows, day=15)["selected_evidence_ids"] == ["0"]
    result = schedule(rows)
    assert result["interval_days"] == 1
    assert result["next_review_at"] == (START + timedelta(days=1)).isoformat()
    assert result["evidence_sufficiency"]["spacing_hours"] == 12


def test_known_difficulty_without_application_cannot_earn_longest_interval():
    rows = [evidence(i, days=i * 2, is_application=False) for i in range(6)]
    assert schedule(rows)["interval_days"] == 7


def test_planner_accepts_one_captured_snapshot_time(workspace):
    factory, space_id, _, _ = workspace
    assert factory().plan_sessions._planning_states(space_id, as_of=START.isoformat()) == {}


def test_evolution_empty_current_scope_is_unknown_and_read_only(workspace):
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    before = state.get_state(space_id)
    result = EvolutionService(state.assessment_service).get(space_id)
    item = next(i for i in result["items"] if i["topic_id"] == topic_id)
    assert item["curve"] == [] and item["events"] == []
    assert item["evidence_sufficiency"]["status"] == "unknown"
    assert factory().get_state(space_id) == before


def test_evolution_revision_and_reset_events_survive_restart(workspace):
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    original = EvolutionService(state.assessment_service).get(space_id)
    state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "Check original grade"})
    after = EvolutionService(factory().assessment_service).get(space_id)
    item = after["items"][0]
    assert item["current"]["next_review_at"] == original["items"][0]["current"]["next_review_at"]
    observations = [e for e in item["events"] if e["type"] == "observation"]
    corrections = [e for e in item["events"] if e["type"] == "correction"]
    assert len(observations) == 5 and len(corrections) == 1
    assert corrections[0]["observed_at"] in {e["observed_at"] for e in observations}
    assert corrections[0]["recorded_at"] >= corrections[0]["observed_at"]
    state.reset_state(space_id, [topic_id], "Fresh start", expected_state_version=2)
    reset = EvolutionService(factory().assessment_service).get(space_id)["items"][0]
    assert reset["curve"] == []
    assert reset["current"]["next_review_at"] is None
    assert any(e["type"] == "reset" for e in reset["events"])


def test_planner_aggregate_and_evolution_use_identical_due(workspace):
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    aggregate = state.get_state(space_id)["items"][0]
    planned = state.plan_sessions._planning_states(space_id)[topic_id]
    evolution = EvolutionService(factory().assessment_service).get(space_id)["items"][0]["current"]
    assert aggregate["next_review_at"] == planned["review_due_at"] == evolution["next_review_at"]


def test_evolution_count_is_bounded_without_altering_current_state(workspace):
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, _, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    service = EvolutionService(state.assessment_service)
    full, limited = service.get(space_id), service.get(space_id, limit=2)
    assert limited["truncated"] is True
    assert sum(len(i["events"]) + len(i["curve"]) for i in limited["items"]) <= 2
    assert limited["items"][0]["current"] == full["items"][0]["current"]


def test_curve_distinguishes_answer_observation_and_original_grade_availability(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, _, _ = workspace
    state = factory()
    monkeypatch.setattr(assessment_module, "now", lambda: START.isoformat())
    assessment = create(state, space_id)
    answer(state, assessment)
    finalized_at = (START + timedelta(days=5)).isoformat()
    monkeypatch.setattr(assessment_module, "now", lambda: finalized_at)
    state.finalize_assessment(assessment["id"], {})
    point = EvolutionService(state.assessment_service).get(space_id)["items"][0]["curve"][0]
    assert point["observed_at"] == START.isoformat()
    assert point["recorded_at"] == finalized_at
    assert point["observed_mastery_score"] == point["mastery_score"] == 1


def test_legacy_attempt_times_are_hydrated_without_modifying_evidence(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    monkeypatch.setattr(assessment_module, "now", lambda: START.isoformat())
    assessment = create(state, space_id)
    answer(state, assessment)
    monkeypatch.setattr(assessment_module, "now", lambda: (START + timedelta(days=5)).isoformat())
    state.finalize_assessment(assessment["id"], {})
    repository = state.assessment_service.repository
    for row in repository.records("evidence", space_id=space_id):
        row.pop("observed_at", None)
        row.pop("observed_at_source", None)
        repository.put_record("evidence", row)
    before = repository.records("evidence", space_id=space_id)
    expected = (START + timedelta(days=1)).isoformat()
    assert factory().plan_sessions._planning_states(space_id)[topic_id]["next_review_at"] == expected
    report = EvolutionService(factory().assessment_service).get(space_id)
    assert report["items"][0]["current"]["next_review_at"] == expected
    assert report["items"][0]["events"][0]["observed_at_source"] == "attempt"
    assert repository.records("evidence", space_id=space_id) == before


def test_planner_does_not_mark_review_due_before_exact_shared_deadline(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    factory, space_id, topic_id, _ = workspace
    state = factory()
    observed = START + timedelta(hours=12)
    monkeypatch.setattr(assessment_module, "now", lambda: observed.isoformat())
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    before_due = observed + timedelta(days=1, seconds=-1)
    before = state.plan_sessions._planning_states(space_id, as_of=before_due.isoformat())[topic_id]
    due = state.plan_sessions._planning_states(space_id, as_of=(before_due + timedelta(seconds=1)).isoformat())[topic_id]
    assert before["review_due"] is False
    assert due["review_due"] is True


def test_correction_reinterprets_one_original_observation_without_a_new_clock():
    rows = [evidence(i, days=i * 2) for i in range(3)]
    original = evidence(3, days=6, score=0)
    audit = (START + timedelta(days=20)).isoformat()
    old = {**original, "eligible": False, "revoked_by_review_id": "review", "revoked_at": audit}
    replacement = {**original, "id": "corrected", "score": 1, "result": "correct",
                   "reviewed_at": audit, "review_id": "review", "observation_id": original["id"],
                   "replaces_evidence_id": original["id"]}
    corrected = schedule([*rows, old, replacement])
    previously_correct = {**original, "score": 1, "result": "correct"}
    expected = schedule([*rows, previously_correct])
    assert corrected["next_review_at"] == expected["next_review_at"]
    assert corrected["stage"] == expected["stage"]
    assert corrected["last_observed_at"] == original["created_at"]
    assert len(corrected["selected_evidence_ids"]) == 4
    revised_wrong = {**replacement, "score": 0, "result": "incorrect"}
    old_correct = {**previously_correct, "eligible": False, "revoked_by_review_id": "review", "revoked_at": audit}
    assert schedule([*rows, old_correct, revised_wrong])["interval_days"] == 1


def test_spacing_is_measured_after_previous_assessment_last_answer():
    rows = [evidence(0, assessment_id="first"), evidence(1, days=2, assessment_id="first"),
            evidence(2, days=2.5)]
    assert schedule(rows)["interval_days"] == 1


def test_later_wrong_answer_anchors_failure_at_its_actual_observation():
    rows = [evidence(0, assessment_id="long-assessment"),
            evidence(1, days=8, score=0, assessment_id="long-assessment"),
            evidence(2, days=5)]
    result = schedule(rows)
    assert result["interval_days"] == 1
    assert result["anchor_observed_at"] == (START + timedelta(days=8)).isoformat()
    assert result["next_review_at"] == (START + timedelta(days=9)).isoformat()


@pytest.mark.parametrize("audit", [{}, {"eligibility_before_revocation": False}])
def test_future_revocation_never_restores_unknown_or_originally_ineligible_evidence(audit):
    row = evidence(0, eligible=False, revoked_at=(START + timedelta(days=10)).isoformat(),
                   revoked_by_review_id="review", **audit)
    assert schedule([row], day=5)["selected_evidence_ids"] == []


def test_revision_change_keeps_all_three_current_review_surfaces_empty(workspace, monkeypatch):
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    assessment = create(state, space_id)
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    bound = state.space_service.bound_topics
    def updated(space):
        return [{**topic, "learning_revision_id": "new-revision"} for topic in bound(space)]
    monkeypatch.setattr(state.space_service, "bound_topics", updated)
    current = state.get_state(space_id)["items"][0]
    planned = state.plan_sessions._planning_states(space_id)[topic_id]
    evolution = EvolutionService(state.assessment_service).get(space_id)["items"][0]
    assert current["next_review_at"] is None
    assert planned["next_review_at"] is None
    assert evolution["current"]["next_review_at"] is None
    assert evolution["curve"] == []
    assert all("obsolete_revision" in event["exclusion_reasons"] for event in evolution["events"])


def test_answer_before_reset_finalized_and_reviewed_later_never_returns_to_curve(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    import knowpath_backend.learning.assessments.grade_reviews as reviews
    from knowpath_backend.learning.assessments.review_policy import effective_evidence
    from knowpath_backend.learning.evolution.service import EvolutionService
    factory, space_id, topic_id, _ = workspace
    state = factory()
    monkeypatch.setattr(assessment_module, "now", lambda: START.isoformat())
    assessment = create(state, space_id)
    answer(state, assessment)
    monkeypatch.setattr(assessment_module, "now", lambda: (START + timedelta(days=1)).isoformat())
    state.reset_state(space_id, [topic_id], "Reset before delayed grading", expected_state_version=0)
    monkeypatch.setattr(assessment_module, "now", lambda: (START + timedelta(days=3)).isoformat())
    state.finalize_assessment(assessment["id"], {})
    monkeypatch.setattr(reviews, "now", lambda: (START + timedelta(days=5)).isoformat())
    state.grade_review(assessment["id"], {"question_id": assessment["questions"][0]["id"], "reason": "Review old epoch"})
    restored = factory()
    report = EvolutionService(restored.assessment_service).get(space_id)["items"][0]
    assert report["curve"] == []
    assert report["current"]["mastery_score"] is None
    assert report["current"]["next_review_at"] is None
    rows = restored.assessment_service.repository.records("evidence", space_id=space_id)
    assert all(not row["eligible"] for row in rows)
    assert effective_evidence(rows, revision_id=rows[0]["topic_revision_id"], epoch=0,
                              as_of=START + timedelta(days=4)) == []


def test_evolution_http_contract_limits_unknown_spaces_and_safe_payload():
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.api.routers.evolution import router
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    app = create_app(question_generator=FixedQuestions())
    path = "/api/v1/learning-spaces/{space_id}/evolution"
    if not any(getattr(route, "path", None) == path for route in app.routes):
        app.include_router(router)
    state = app.state.learning_state
    material = state.material_service.create(filename="notes.md", content=b"# Functions\n\nReusable behavior.", idempotency_key="material")
    space = state.create_space({"name": "Python", "material_ids": [material.material.id]})
    assessment = create(state, space["id"])
    answer(state, assessment)
    state.finalize_assessment(assessment["id"], {})
    with TestClient(app) as client:
        response = client.get(path.format(space_id=space["id"]))
        assert response.status_code == 200
        assert response.json()["policy_version"] == "review-v2"
        assert all(secret not in response.text for secret in ("answer_key", "rubric", "source_text"))
        assert client.get(path.format(space_id=space["id"]), params={"limit": 501}).status_code == 422
        assert client.get(path.format(space_id="unknown")).status_code == 404


def test_legacy_hydration_reads_immutable_attempts_without_inverting_space_lock_order():
    from knowpath_backend.learning.assessments.review_policy import enrich_observation_times
    calls = []
    class Repository:
        def records(self, table, *, lock=True, **filters):
            calls.append((table, lock, filters))
            return [{"id": "attempt", "assessment_id": "assessment", "question_id": "question",
                     "created_at": START.isoformat()}]
    row = evidence(1, days=10, assessment_id="assessment", attempt_id="attempt", question_id="question")
    hydrated = enrich_observation_times(Repository(), [row])
    assert calls == [("attempts", False, {"assessment_id": "assessment"})]
    assert hydrated[0]["observed_at"] == START.isoformat()
    assert "observed_at" not in row


def test_legacy_mastered_cache_revalidates_real_spacing_without_writes(workspace, monkeypatch):
    import knowpath_backend.learning.assessments.service as assessment_module
    from knowpath_backend.learning.assessments.mastery import aggregate
    from knowpath_backend.learning.evolution.service import EvolutionService
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    factory, space_id, topic_id, _ = workspace
    state = factory()
    clock = [START.isoformat()]
    monkeypatch.setattr(assessment_module, "now", lambda: clock[0])
    first = create(state, space_id, key="first")
    answer(state, first, key="first-answer")
    clock[0] = (START + timedelta(minutes=30)).isoformat()
    state.finalize_assessment(first["id"], {})
    class DifferentQuestions(FixedQuestions):
        def generate(self, topics, payload):
            return [{**q, "prompt": q["prompt"] + " using an alternative example"}
                    for q in super().generate(topics, payload)]
    state.assessment_service.generator = DifferentQuestions()
    clock[0] = (START + timedelta(hours=1)).isoformat()
    second = create(state, space_id, key="second")
    answer(state, second, key="second-answer")
    clock[0] = (START + timedelta(days=2, minutes=30)).isoformat()
    state.finalize_assessment(second["id"], {})
    repository = state.assessment_service.repository
    rows = repository.records("evidence", space_id=space_id)
    for item in rows:
        item.pop("observed_at", None)
        item.pop("observed_at_source", None)
        repository.put_record("evidence", item)
    stored = repository.records("states", space_id=space_id)[0]
    legacy = aggregate(topic_id, rows[0]["topic_revision_id"], rows, {}, 2, clock[0])
    assert legacy["status"] == "mastered"  # Old finalization-time spacing.
    legacy.update(id=stored["id"], space_id=space_id, epoch=0)
    repository.put_record("states", legacy)
    before_states = repository.records("states", space_id=space_id)
    before_evidence = repository.records("evidence", space_id=space_id)
    restarted = factory()
    current = restarted.get_state(space_id)["items"][0]
    evolution = EvolutionService(restarted.assessment_service).get(space_id)["items"][0]["current"]
    planned = restarted.plan_sessions._planning_states(space_id, as_of=clock[0])[topic_id]
    for projected in (current, evolution):
        assert projected["evidence_sufficiency"]["spacing_hours"] == 1
        assert projected["evidence_sufficiency"]["status"] == "insufficient"
        assert projected["status"] == "needs_review"  # Existing due-time overlay.
    assert planned["status"] == "learning"
    assert planned["mastery_score"] == 1.0
    plan = restarted.create_plan(space_id, {"include_review": False})
    assert any(task["topic_ids"] == [topic_id] and task["kind"] == "learn" for task in plan["tasks"])
    assert repository.records("states", space_id=space_id) == before_states
    assert repository.records("evidence", space_id=space_id) == before_evidence


def test_current_projection_honors_active_mastery_spacing_policy():
    from knowpath_backend.learning.assessments.mastery import MasteryPolicy
    from knowpath_backend.learning.assessments.review_policy import project_review_state
    rows = [evidence(0, assessment_id="first"), evidence(1, assessment_id="first"), evidence(2, days=1)]
    state = {"topic_id": "t", "topic_revision_id": "r", "epoch": 0, "status": "mastered",
             "mastery_score": 1.0, "score_validity": "current", "state_version": 1,
             "last_assessed_at": rows[-1]["created_at"]}
    result = project_review_state(state, rows, revision_id="r", epoch=0, as_of=START + timedelta(days=1),
                                  mastery_policy=MasteryPolicy(minimum_spacing_hours=48))
    assert result["status"] == "learning"
    assert result["evidence_sufficiency"]["status"] == "insufficient"
    assert "insufficient_observation_spacing" in result["evidence_sufficiency"]["missing"]


def test_current_projection_preserves_stale_and_unstable_semantics():
    from knowpath_backend.learning.assessments.mastery import MasteryPolicy
    from knowpath_backend.learning.assessments.review_policy import project_review_state
    rows = [evidence(0, score=0), evidence(1, days=1, score=0)]
    base = {"topic_id": "t", "topic_revision_id": "r", "epoch": 0, "status": "unstable",
            "mastery_score": 0, "score_validity": "current", "state_version": 1,
            "last_assessed_at": rows[-1]["created_at"]}
    args = {"revision_id": "r", "epoch": 0, "as_of": START + timedelta(days=1), "mastery_policy": MasteryPolicy()}
    assert project_review_state(base, rows, **args)["status"] == "unstable"
    stale = project_review_state({**base, "score_validity": "stale", "status": "needs_review"}, rows, **args)
    assert stale["score_validity"] == "stale" and stale["status"] == "needs_review"
    reset = project_review_state(base, rows, **{**args, "epoch": 4})
    assert reset["score_validity"] == "stale" and reset["next_review_at"] is None
