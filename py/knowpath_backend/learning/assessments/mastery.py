"""Versioned, conservative mastery-v1 aggregation over immutable evidence."""
from datetime import timedelta
from dataclasses import dataclass

from knowpath_backend.learning.assessments.review_policy import review_schedule, evidence_sufficiency, moment, observation_time


@dataclass(frozen=True)
class MasteryPolicy:
    version: str = "mastery-v1"
    window: int = 20
    minimum_families: int = 3
    minimum_assessments: int = 2
    minimum_spacing_hours: int = 24
    mastered_threshold: float = 0.8
    review_after_days: int = 7

    def __post_init__(self):
        if self.window < 1 or self.minimum_families < 1 or self.minimum_assessments < 1 or self.minimum_spacing_hours < 0 or self.review_after_days < 1 or not 0 <= self.mastered_threshold <= 1:
            raise ValueError("Invalid mastery policy")


def aggregate(topic_id, revision_id, evidence, previous, state_version, assessed_at, *, policy=None):
    policy = policy or MasteryPolicy()
    eligible = [e for e in evidence if e.get("eligible") and e.get("topic_revision_id") == revision_id
                and e.get("score") is not None and not e.get("assisted") and not e.get("revoked_by_review_id")]
    first_by_family = {}
    for item in sorted(eligible, key=lambda e: (e.get("submission_sequence", 0), e["created_at"], e.get("observation_id", e["id"]))):
        first_by_family.setdefault(item["family_id"], item)
    selected = list(first_by_family.values())[-policy.window:]
    score = sum(e["score"] for e in selected) / len(selected) if selected else None
    status = "learning" if evidence else "unseen"
    if score is not None:
        moments = [observation_time(e) for e in selected]
        spaced = max(moments) - min(moments) >= timedelta(hours=policy.minimum_spacing_hours)
        assessments = {e["assessment_id"] for e in selected}
        if score >= policy.mastered_threshold and len(selected) >= policy.minimum_families and len(assessments) >= policy.minimum_assessments and spaced and any(e.get("is_application") for e in selected):
            status = "mastered"
        elif previous.get("status") in {"mastered", "unstable", "needs_review"} and score < policy.mastered_threshold:
            status = "unstable"
    # Erasure may intentionally leave neither observations nor an assessment
    # time. Keep that unknown state; a wall-clock fallback would invent history.
    timestamps = [assessed_at, *[e["created_at"] for e in evidence],
                  *[e["reviewed_at"] for e in evidence if e.get("reviewed_at")]]
    cutoff = max((moment(value) for value in timestamps if value is not None), default=None)
    epoch = evidence[0].get("epoch", 0) if evidence else previous.get("epoch", 0)
    schedule = review_schedule(evidence, revision_id=revision_id, epoch=epoch, as_of=cutoff)
    sufficiency = evidence_sufficiency(selected, minimum_families=policy.minimum_families,
        minimum_assessments=policy.minimum_assessments, minimum_spacing_hours=policy.minimum_spacing_hours)
    return {"topic_id": topic_id, "topic_revision_id": revision_id,
            "mastery_score": score, "score_validity": "current" if score is not None else "unverified",
            "status": status, "evidence_ids": [e["id"] for e in selected],
            "evidence_count": len(evidence), "independent_evidence_count": len(selected),
            "error_tags": sorted({tag for e in selected for tag in e["error_tags"]}),
            "last_assessed_at": assessed_at, "next_review_at": schedule["next_review_at"],
            "review_schedule": schedule, "evidence_sufficiency": sufficiency,
            "state_version": state_version, "policy_version": policy.version}
