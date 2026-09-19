from datetime import datetime, timedelta, timezone

from knowpath_backend.learning.mastery import aggregate, MasteryPolicy


START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def row(i, *, family=None, score=1.0, hours=0, assessment="a", **extra):
    return {"id": str(i), "family_id": family or str(i), "score": score, "created_at": (START + timedelta(hours=hours)).isoformat(), "assessment_id": assessment, "topic_revision_id": "r1", "eligible": True, "assisted": False, "error_tags": [], "is_application": True, **extra}


def estimate(evidence, previous=None, policy=None):
    return aggregate("t1", "r1", evidence, previous or {}, 1, START.isoformat(), policy=policy or MasteryPolicy())


def test_mastery_requires_independent_spaced_assessments_and_application():
    evidence = [row(1), row(2), row(3, assessment="b", hours=24)]
    result = estimate(evidence)
    assert result["status"] == "mastered"
    assert result["mastery_score"] == 1.0
    assert result["next_review_at"] is not None
    assert estimate(evidence[:2])["status"] == "learning"
    assert estimate([dict(e, assessment_id="a") for e in evidence])["status"] == "learning"
    assert estimate([dict(e, created_at=START.isoformat()) for e in evidence])["status"] == "learning"
    assert estimate([dict(e, is_application=False) for e in evidence])["status"] == "learning"


def test_first_valid_family_submission_wins_and_ineligible_evidence_is_ignored():
    evidence = [row(1, family="same", score=0), row(2, family="same", hours=48), row(3, assisted=True), row(4, score=None), row(5, topic_revision_id="old"), row(6, eligible=False)]
    result = estimate(evidence)
    assert result["mastery_score"] == 0.0
    assert result["independent_evidence_count"] == 1
    assert result["evidence_ids"] == ["1"]
    assert estimate(evidence[2:])["mastery_score"] is None


def test_last_twenty_families_and_configurable_thresholds():
    evidence = [row(i, hours=i, assessment=str(i), score=0 if i < 5 else 1.0) for i in range(25)]
    result = estimate(evidence)
    assert result["independent_evidence_count"] == 20
    assert result["mastery_score"] == 1.0
    assert estimate(evidence, policy=MasteryPolicy(window=25))["mastery_score"] == 0.8
    assert estimate(evidence, policy=MasteryPolicy(minimum_spacing_hours=48))["status"] == "learning"


def test_mastered_state_becomes_unstable_on_valid_lower_score():
    result = estimate([row(1, score=0)], previous={"status": "mastered"})
    assert result["status"] == "unstable"
    assert estimate([])["status"] == "unseen"


def test_review_keeps_observation_order_at_window_boundary():
    evidence = [row(f"{i:02}", score=0 if i == 0 else 1, submission_sequence=1) for i in range(25)]
    before = estimate(evidence)
    replacement = dict(evidence[0], id="zz-new-review", observation_id=evidence[0]["id"])
    after = estimate([replacement, *evidence[1:]])
    assert after["mastery_score"] == before["mastery_score"]
    assert after["evidence_ids"] == before["evidence_ids"]
    assert after["next_review_at"] == before["next_review_at"]
