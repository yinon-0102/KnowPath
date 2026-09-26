"""Decision-time topic replay and observational outcomes, without causal claims.

Historical corrections have their own availability time. Real answer timestamps
determine retest spacing; late grading and regrading never create delayed tests.
All three policies receive the same frozen topics and pre-decision evidence.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from knowpath_backend.learning.assessments.adaptive import rank_topics
from knowpath_backend.learning.assessments.mastery import aggregate
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.evaluation.schemas import ReplayRequest
from knowpath_backend.learning.spaces.service import now

VERSION = "historical-topic-replay-v1"
MAX_HISTORY = 20000
MAX_DECISION_TOPICS = 2048
MAX_ROW_VISITS = 2000000
POLICIES = ("fixed_order", "existing_rules_topic_proxy", "adaptive_topic_proxy")


def _time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _available(row):
    return _time(row.get("reviewed_at") or row["created_at"])


def evidence_as_of(rows, cutoff, *, strict=False):
    """Choose one available version per observation; do not infer missing audits."""
    cutoff = _time(cutoff)
    before = (lambda value: value < cutoff) if strict else (lambda value: value <= cutoff)
    candidates = [row for row in rows if before(_available(row))]
    replaced = {row["replaces_evidence_id"] for row in candidates if row.get("replaces_evidence_id")}
    grouped = {}
    for item in candidates:
        if item["id"] in replaced:
            continue
        row = deepcopy(item)
        if row.get("revoked_by_review_id"):
            revoked_at = row.get("revoked_at")
            if not revoked_at or before(_time(revoked_at)):
                continue
            # Older deployments did not retain original eligibility. Those
            # ambiguous rows stay excluded instead of inventing old evidence.
            row["eligible"] = row.get("eligibility_before_revocation", False)
            row.pop("revoked_by_review_id", None)
            row.pop("revoked_at", None)
        key = (row.get("assessment_id"), row.get("question_id", row.get("observation_id", row["id"])))
        previous = grouped.get(key)
        if previous is None or (_available(row), row["id"]) > (_available(previous), previous["id"]):
            grouped[key] = row
    return sorted(grouped.values(), key=lambda row: (_time(row["created_at"]), row["id"]))


def _independent(row):
    score = row.get("score")
    return (row.get("eligible", False) and not row.get("assisted")
            and not row.get("revoked_by_review_id") and isinstance(score, (int, float))
            and not isinstance(score, bool) and 0 <= score <= 1
            and row.get("question_review_status", "accepted") not in {"pending", "invalid"})


def _rules_order(topics, states, cutoff):
    """Planner-v2 priority/Kahn ordering on the reconstructed decision state."""
    rank = {"needs_review": 0, "unstable": 1, "learning": 2, "mastered": 3, "unseen": 4}
    remaining = {topic["id"]: topic for topic in topics}
    known, result, blocked = set(remaining), [], []
    def priority(topic):
        state = states[topic["id"]]
        status = state["status"]
        due = state.get("next_review_at")
        if status == "mastered" and due and _time(due).date() <= cutoff.date():
            status = "needs_review"
        return (rank.get(status, 4), -len(state.get("error_tags", [])),
                state.get("last_assessed_at") or "9999", topic["id"])
    while remaining:
        ready = [topic for topic in remaining.values() if all(parent in known and parent not in remaining
                 for parent in topic.get("prerequisites", []))]
        if not ready:
            blocked = sorted(remaining)
            break
        selected = min(ready, key=priority)
        result.append(selected["id"]); remaining.pop(selected["id"])
    return result, blocked


def _retests(rows, cutoff, minimum_delay_hours, start, limit):
    by_topic = defaultdict(list)
    for row in rows:
        if not row.get("observed_at") or row.get("observed_at_source") == "legacy_finalization":
            continue
        observed = _time(row["observed_at"])
        if observed > cutoff or observed > _time(row["created_at"]):
            continue
        by_topic[(row["topic_id"], row["topic_revision_id"], row.get("epoch", 0))].append(row)
    pairs = []
    for key, observations in sorted(by_topic.items()):
        anchor, families = None, set()
        for row in sorted(observations, key=lambda e: (_time(e["observed_at"]), e["id"])):
            family = row.get("family_id")
            repeated = family in families
            if family:
                families.add(family)
            if not _independent(row) or not family or repeated:
                # Known hints, invalid observations and repeat practice break
                # the consecutive independent retest chain.
                anchor = None
                continue
            if anchor is None:
                anchor = row
                continue
            delay = (_time(row["observed_at"]) - _time(anchor["observed_at"])).total_seconds() / 3600
            if row["assessment_id"] == anchor["assessment_id"] or delay < minimum_delay_hours:
                anchor = row
                continue
            if start is None or _time(row["observed_at"]) >= start:
                pairs.append({"topic_id": key[0], "topic_revision_id": key[1], "epoch": key[2],
                    "baseline_evidence_id": anchor["id"], "followup_evidence_id": row["id"],
                    "baseline_score": anchor["score"], "followup_score": row["score"],
                    "score_change": row["score"] - anchor["score"], "delay_hours": delay,
                    "observed_at": row["observed_at"]})
            anchor = row
    pairs.sort(key=lambda pair: (_time(pair["observed_at"]), pair["followup_evidence_id"]))
    n = len(pairs)
    return {"pair_count": n, "mean_score_change": sum(p["score_change"] for p in pairs) / n if n else None,
            "mean_followup_score": sum(p["followup_score"] for p in pairs) / n if n else None,
            "minimum_delay_hours": minimum_delay_hours, "pairs": pairs[-limit:],
            "truncated": n > limit, "reason": None if n else "insufficient_independent_delayed_observations",
            "interpretation": "Consecutive recorded independent different-family answer changes, not causal policy effects or calibrated retention. Known assisted/repeated practice breaks pairing."}


def build_replay(assessments, evidence, *, as_of, limit=200, minimum_delay_hours=24, from_time=None, to_time=None):
    cutoff = min(_time(as_of), _time(to_time)) if to_time else _time(as_of)
    start = _time(from_time) if from_time else None
    historical = sorted([a for a in assessments if a.get("status") == "completed"
        and (a.get("result") or {}).get("graded_at")
        and _time(a["result"]["graded_at"]) <= cutoff
        and _time(a["created_at"]) <= cutoff and (start is None or _time(a["created_at"]) >= start)],
        key=lambda a: (_time(a["created_at"]), a["id"]))
    selected_assessments = historical[-limit:]
    topic_count = sum(len(a.get("snapshot", {}).get("topics", [])) for a in selected_assessments)
    row_visits = len(evidence) * (len(selected_assessments) + topic_count)
    if (len(assessments) + len(evidence) > MAX_HISTORY or topic_count > MAX_DECISION_TOPICS
            or row_visits > MAX_ROW_VISITS):
        raise DomainConflict("REPLAY_LIMIT_EXCEEDED", "回放组合计算量过大，请缩小 limit 或决策时间范围",
            {"max_decision_topics": MAX_DECISION_TOPICS, "max_row_visits": MAX_ROW_VISITS,
             "requested_decision_topics": topic_count, "estimated_row_visits": row_visits})
    current_rows = evidence_as_of(evidence, cutoff)
    decisions, errors = [], []
    target_count = 0
    totals = {name: {"policy": name, "decision_count": 0, "weak_or_unknown_choices": 0,
                      "prerequisite_choices": 0, "different_from_fixed": 0} for name in POLICIES}
    for assessment in selected_assessments:
        decision_time = _time(assessment["created_at"])
        snapshot = assessment.get("snapshot", {})
        topics, epochs = snapshot.get("topics", []), snapshot.get("epochs", {})
        if len(topics) > 512:
            raise DomainConflict("REPLAY_LIMIT_EXCEEDED", "单次历史测验主题过多，请缩小回放范围")
        prefix = [row for row in evidence_as_of(evidence, decision_time, strict=True)
                  if row["assessment_id"] != assessment["id"] and _independent(row)]
        states = {}
        for topic in topics:
            selected = [row for row in prefix if row["topic_id"] == topic["id"]
                and row["topic_revision_id"] == topic["revision_id"] and row.get("epoch", 0) == epochs.get(topic["id"], 0)]
            # Shared conservative mastery calculation; historic `unstable`
            # transitions are not reconstructed from mutable current states.
            state = aggregate(topic["id"], topic["revision_id"], selected, {}, 0, assessment["created_at"])
            state["last_assessed_at"] = max((row["created_at"] for row in selected), default=None)
            states[topic["id"]] = state
        fixed = [topic["id"] for topic in topics]
        rules, blocked = _rules_order(topics, states, decision_time)
        adaptive = rank_topics(topics, states)
        rankings = {"fixed_order": fixed, "existing_rules_topic_proxy": rules,
                    "adaptive_topic_proxy": [row["topic_id"] for row in adaptive]}
        prerequisites = {parent for topic in topics for parent in topic.get("prerequisites", [])}
        choices = {name: values[0] if values else None for name, values in rankings.items()}
        for name, chosen in choices.items():
            if chosen is None:
                continue
            state = states[chosen]; score = state["mastery_score"]
            totals[name]["decision_count"] += 1
            totals[name]["weak_or_unknown_choices"] += int(score is None or score < .8 or state["independent_evidence_count"] < 3)
            totals[name]["prerequisite_choices"] += int(chosen in prerequisites)
            totals[name]["different_from_fixed"] += int(chosen != choices["fixed_order"])
        projections = {identifier: {key: state[key] for key in ("mastery_score", "status",
            "independent_evidence_count", "evidence_ids")} for identifier, state in states.items()}
        decisions.append({"assessment_id": assessment["id"], "decision_at": assessment["created_at"],
            "candidate_topic_ids": fixed, "scope_version": snapshot.get("scope_version"),
            "states": projections, "choices": choices, "rankings": rankings,
            "rule_blocked_topic_ids": blocked, "adaptive_reasons": adaptive})
        by_topic = {topic["id"]: topic for topic in topics}
        families = {row["family_id"] for row in prefix}
        for row in current_rows:
            topic = by_topic.get(row["topic_id"])
            if (row["assessment_id"] != assessment["id"] or not topic or not _independent(row)
                or _time(row["created_at"]) <= decision_time or row.get("family_id") in families
                or row.get("epoch", 0) != epochs.get(topic["id"], 0)
                or row["topic_revision_id"] != topic["revision_id"]):
                continue
            families.add(row["family_id"]); target_count += 1
            estimate = states[topic["id"]]["mastery_score"]
            if estimate is not None:
                errors.append(abs(estimate - row["score"]))
    observed = _retests(current_rows, cutoff, minimum_delay_hours, start, limit)
    return {"policy_version": VERSION, "as_of": cutoff.isoformat(), "from_time": from_time,
        "causal_effect_estimated": False, "policies": list(totals.values()), "decisions": decisions,
        "prediction": {"predictor": "mastery-v1-prefix-evidence-mean", "predicted_observations": len(errors),
            "eligible_observations": target_count, "coverage_fraction": len(errors) / target_count if target_count else None,
            "mean_absolute_error": sum(errors) / len(errors) if errors else None,
            "reason": None if errors else "insufficient_prior_independent_evidence"},
        "observed_retests": observed, "truncated": len(historical) > limit,
        "limits": {"decision_limit": limit, "available_decisions": len(historical), "history_row_limit": MAX_HISTORY,
                   "max_decision_topics": MAX_DECISION_TOPICS, "max_row_visits": MAX_ROW_VISITS},
        "snapshot_hash": _digest({"decisions": decisions, "evidence": [{k: row.get(k) for k in ("id",
            "score", "eligible", "assisted", "reviewed_at", "observed_at")} for row in current_rows]}),
        "limitations": ["Policy comparisons replay only the first topic priority at each assessment creation, not question-level exposure or counterfactual answers.",
            "Only assessments completed by the report cutoff enter policy comparison; records missing completion timestamps are excluded.",
            "Existing-rule ordering is a topic proxy using shared reconstructed mastery; historic unstable transitions and previous planner task completions are not reconstructed.",
            "Adaptive ranking reuses adaptive-v1, but frozen pool coverage and within-assessment prerequisite probes are not simulated.",
            "Scores are evidence averages, not calibrated probabilities. Prediction error is shared by policies and is not a learning gain.",
            "Retests need recorded answer times; historical corrections are applied to outcomes only when available by the report cutoff.",
            "Legacy revoked rows without original eligibility audits are conservatively excluded. Same-time prior evidence is excluded from decisions.",
            "Retest observations are affected by question difficulty, self-selection and other study activity; no causal comparison is possible."]}


class ReplayService:
    def __init__(self, assessments):
        self.assessments = assessments

    def replay(self, space_id, payload, idempotency_key=None):
        payload = ReplayRequest.model_validate(payload).model_dump()
        def change():
            service = self.assessments
            space = service.spaces.repository.get(space_id)
            repo = service.repository
            assessments = repo.records("assessments", space_id=space_id, lock=False)
            evidence = repo.records("evidence", space_id=space_id, lock=False)
            if len(assessments) + len(evidence) > MAX_HISTORY:
                raise DomainConflict("REPLAY_LIMIT_EXCEEDED", "历史数据超出在线回放上限，需要离线评估")
            # Read immutable attempt times without acquiring assessment locks
            # after the space lock. A concurrent writer must lock this space.
            for row in evidence:
                if row.get("observed_at") is None and row.get("attempt_id"):
                    attempt = repo.get_record("attempts", row["attempt_id"], lock=False)
                    if (attempt.get("assessment_id") == row.get("assessment_id")
                            and attempt.get("question_id") == row.get("question_id")):
                        row["observed_at"] = attempt["created_at"]
                        row["observed_at_source"] = "attempt"
            report = build_replay(assessments, evidence, as_of=now(), **payload)
            return {"space_id": space_id, "state_version": space["state_version"], **report}
        return self.assessments._execute("policy.replay", space_id, payload, idempotency_key, change)
