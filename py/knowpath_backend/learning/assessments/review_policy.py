"""Deterministic review-v2 rules over independent, time-stamped observations.

These are transparent scheduling bounds, not calibrated memory probabilities.
Corrections change the interpretation of an observation, never its timestamp.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone


REVIEW_VERSION = "review-v2"
REVIEW_CONFIG = {"interval_days": [1, 3, 7, 14], "minimum_spacing_hours": 24,
                 "window": 20, "easy_or_unknown_max_stage": 1,
                 "known_difficulty_max_stage": 2, "hard_application_max_stage": 3,
                 "success_score": 1.0, "assessment_success": "all_selected_answers_correct",
                 "success_anchor": "first_independent_answer_in_assessment",
                 "failure_anchor": "latest_incorrect_answer_in_assessment"}
LIMITATIONS = ["Deterministic scheduling rule; not a calibrated retention probability.",
              "Difficulty and application labels describe frozen questions, not validated item difficulty.",
              "Missing difficulty stays unknown and uses the conservative three-day cap.",
              "Historical revocations with no recorded prior eligibility stay excluded instead of inferring eligibility.",
              "A corrected historical score can change the retrospective due date, but never creates a new observation or uses review time as learning time."]


def moment(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def observation_time(item):
    return moment(item.get("observed_at") or item["created_at"])


def enrich_observation_times(repository, evidence):
    """Hydrate legacy answer times from immutable attempts without writing data."""
    rows = deepcopy(evidence)
    required = {e.get("assessment_id") for e in rows if not e.get("observed_at") and e.get("attempt_id")}
    # State/planner/evolution readers may already hold the space lock. Answer
    # commands lock attempts before space, so never lock in the reverse order.
    # Attempts are immutable revisions; a plain read of the frozen ID is enough.
    attempts = {a["id"]: a for assessment_id in required if assessment_id
                for a in repository.records("attempts", assessment_id=assessment_id, lock=False)}
    for row in rows:
        if row.get("observed_at"):
            continue
        attempt = attempts.get(row.get("attempt_id"))
        if attempt and attempt.get("assessment_id") == row.get("assessment_id") and attempt.get("question_id") == row.get("question_id"):
            row.update(observed_at=attempt["created_at"], observed_at_source="attempt")
        else:
            row.update(observed_at=row["created_at"], observed_at_source="legacy_finalization")
    return rows


def effective_evidence(evidence, *, revision_id, epoch, as_of):
    """Return only observations valid at the cutoff, without future corrections."""
    cutoff = moment(as_of)
    result = []
    for original in evidence:
        if original.get("topic_revision_id") != revision_id or original.get("epoch", 0) != epoch:
            continue
        if not original.get("created_at") or moment(original["created_at"]) > cutoff:
            continue
        if observation_time(original) > cutoff:
            continue
        if original.get("reviewed_at") and moment(original["reviewed_at"]) > cutoff:
            continue
        item = deepcopy(original)
        if item.get("revoked_by_review_id"):
            if not item.get("revoked_at") or moment(item["revoked_at"]) <= cutoff:
                continue
            # Never infer old eligibility from a score: reset/stale grades can
            # have scores and still have been ineligible before the review.
            item["eligible"] = item.get("eligibility_before_revocation") is True
            item.pop("revoked_by_review_id", None)
            item.pop("revoked_at", None)
        if not item.get("eligible") or item.get("assisted") or item.get("score") is None:
            continue
        if item.get("question_review_status") in {"pending", "invalid"}:
            continue
        result.append(item)
    return result


def independent_evidence(evidence, *, window=20):
    """Keep mastery-v1's first valid submission for each question family."""
    families = {}
    for item in sorted(evidence, key=lambda e: (e.get("submission_sequence", 0), observation_time(e), e.get("observation_id", e["id"]))):
        families.setdefault(item.get("family_id", item.get("observation_id", item["id"])), item)
    return list(families.values())[-window:]


def evidence_sufficiency(selected, *, minimum_families=3, minimum_assessments=2, minimum_spacing_hours=24):
    times = [observation_time(e) for e in selected]
    assessments = {e.get("assessment_id") for e in selected if e.get("assessment_id")}
    hours = (max(times) - min(times)).total_seconds() / 3600 if times else 0
    missing = []
    if len(selected) < minimum_families:
        missing.append("insufficient_independent_families")
    if len(assessments) < minimum_assessments:
        missing.append("insufficient_distinct_assessments")
    if hours < minimum_spacing_hours:
        missing.append("insufficient_observation_spacing")
    if not any(e.get("is_application") for e in selected):
        missing.append("no_application_observation")
    return {"status": "unknown" if not selected else "insufficient" if missing else "sufficient",
            "independent_families": len(selected), "distinct_assessments": len(assessments),
            "spacing_hours": hours, "has_application": any(e.get("is_application") for e in selected),
            "unknown_difficulty_count": sum(e.get("difficulty") not in {"easy", "medium", "hard"} for e in selected),
            "legacy_observation_time_count": sum(not e.get("observed_at") or e.get("observed_at_source") == "legacy_finalization" for e in selected),
            "missing": missing, "meaning": "Evidence coverage, not probability of mastery."}


def review_schedule(evidence, *, revision_id, epoch, as_of):
    """Compute one bounded review schedule using only data available by as_of."""
    if as_of is None and evidence:
        raise ValueError("as_of is required when evidence is present")
    cutoff = moment(as_of) if as_of is not None else None
    effective = effective_evidence(evidence, revision_id=revision_id, epoch=epoch, as_of=cutoff) if evidence else []
    selected = independent_evidence(effective,
                                    window=REVIEW_CONFIG["window"])
    groups = {}
    for item in selected:
        groups.setdefault(item.get("assessment_id") or item.get("observation_id", item["id"]), []).append(item)
    def group_observed(rows):
        failures = [observation_time(e) for e in rows if e["score"] < REVIEW_CONFIG["success_score"]]
        return max(failures) if failures else min(observation_time(e) for e in rows)
    ordered = sorted(groups.items(), key=lambda pair: (group_observed(pair[1]), str(pair[0])))
    stage, anchor, anchor_assessment, last_assessment_end = 0, None, None, None
    reasons = []
    for assessment_id, rows in ordered:
        observed = group_observed(rows)
        assessment_end = max(observation_time(e) for e in rows)
        spaced = last_assessment_end is None or observed - last_assessment_end >= timedelta(hours=REVIEW_CONFIG["minimum_spacing_hours"])
        last_assessment_end = max(last_assessment_end or assessment_end, assessment_end)
        if any(e["score"] < REVIEW_CONFIG["success_score"] for e in rows):
            stage, anchor, anchor_assessment = 0, observed, assessment_id
            reasons.append("independent_failure")
            continue
        if anchor is None:
            anchor, anchor_assessment = observed, assessment_id
            reasons.append("first_independent_success")
            continue
        if assessment_id == anchor_assessment or not spaced:
            reasons.append("unspaced_success_does_not_extend")
            continue
        cap = 1
        if any(e.get("difficulty") in {"medium", "hard"} for e in rows):
            cap = 2
        if any(e.get("difficulty") == "hard" and e.get("is_application") for e in rows):
            cap = 3
        if any(e.get("difficulty") not in {"easy", "medium", "hard"} for e in rows):
            reasons.append("unknown_difficulty_conservative_cap")
        if cap == 1:
            reasons.append("easy_or_unknown_three_day_cap")
        elif cap == 2:
            reasons.append("hard_application_required_for_fourteen_days")
        stage = min(stage + 1, cap)
        anchor, anchor_assessment = observed, assessment_id
        reasons.append("spaced_independent_success")
    if not selected:
        reasons.append("no_current_independent_evidence")
    elif any(e.get("difficulty") not in {"easy", "medium", "hard"} for e in selected):
        reasons.append("unknown_difficulty_conservative_cap")
    if any(not e.get("observed_at") or e.get("observed_at_source") == "legacy_finalization" for e in selected):
        reasons.append("legacy_finalization_time_fallback")
    interval = REVIEW_CONFIG["interval_days"][stage] if anchor else None
    due = anchor + timedelta(days=interval) if anchor else None
    return {"policy_version": REVIEW_VERSION, "stage": stage if anchor else None,
            "interval_days": interval, "next_review_at": due.isoformat() if due else None,
            "due": bool(due and due <= cutoff),
            "last_observed_at": max((observation_time(e) for e in selected), default=None).isoformat() if selected else None,
            "anchor_observed_at": anchor.isoformat() if anchor else None,
            "selected_evidence_ids": [e["id"] for e in selected],
            "reasons": list(dict.fromkeys(reasons)), "evidence_sufficiency": evidence_sufficiency(selected),
            "config": deepcopy(REVIEW_CONFIG), "limitations": list(LIMITATIONS)}


def project_mastery_state(state, evidence, *, revision_id, epoch, as_of, mastery_policy=None):
    """Revalidate cached mastery using actual answer times, without persistence.

    The import is local because mastery aggregation also consumes review-v2.
    The cached state remains the prior-status input for mastery-v1's unstable
    transition; knowledge/reset staleness always takes precedence.
    """
    from knowpath_backend.learning.assessments.mastery import aggregate, MasteryPolicy
    policy = mastery_policy or MasteryPolicy()
    result = deepcopy(state)
    eligible = effective_evidence(evidence, revision_id=revision_id, epoch=epoch, as_of=as_of)
    selected = independent_evidence(eligible, window=policy.window)
    result["evidence_sufficiency"] = evidence_sufficiency(selected,
        minimum_families=policy.minimum_families, minimum_assessments=policy.minimum_assessments,
        minimum_spacing_hours=policy.minimum_spacing_hours)
    if state.get("topic_revision_id") != revision_id or state.get("epoch", 0) != epoch:
        result.update(score_validity="stale", status="needs_review")
        return result
    if state.get("score_validity") == "stale" or not evidence:
        return result
    rebuilt = aggregate(state["topic_id"], revision_id, eligible, state,
        state.get("state_version", 0), state.get("last_assessed_at"), policy=policy)
    if not eligible and any(e.get("topic_revision_id") == revision_id and e.get("epoch", 0) == epoch
            and moment(e["created_at"]) <= moment(as_of)
            and (not e.get("reviewed_at") or moment(e["reviewed_at"]) <= moment(as_of)) for e in evidence):
        rebuilt["status"] = "learning"  # Observed but unverified/assisted.
    for field in ("status", "mastery_score", "score_validity", "evidence_ids",
                  "independent_evidence_count", "error_tags", "evidence_sufficiency"):
        result[field] = rebuilt[field]
    return result


def project_review_state(state, evidence, *, revision_id, epoch, as_of, mastery_policy=None):
    """Read-only projection shared by state, planner, and evolution surfaces."""
    result = project_mastery_state(state, evidence, revision_id=revision_id,
        epoch=epoch, as_of=as_of, mastery_policy=mastery_policy)
    schedule = review_schedule(evidence, revision_id=revision_id, epoch=epoch, as_of=as_of)
    result.update(next_review_at=schedule["next_review_at"], review_due_at=schedule["next_review_at"],
                  review_due=schedule["due"], review_schedule=schedule)
    if state.get("topic_revision_id") != revision_id or state.get("epoch", 0) != epoch:
        result.update(score_validity="stale", status="needs_review")
    elif schedule["due"]:
        result["status"] = "needs_review"
    return result
