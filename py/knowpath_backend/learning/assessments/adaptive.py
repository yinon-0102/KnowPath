"""Deterministic, source-scoped diagnostic ordering over a frozen question pool.

Scores are evidence means and priorities are rules, not calibrated probabilities.
A supported prerequisite gap is an observation to investigate, never a causal
claim about the dependent error or permission to modify the knowledge graph.
"""
from copy import deepcopy

from knowpath_backend.learning.assessments.mastery import aggregate

POLICY_VERSION = "adaptive-v1"
MAX_RUNTIME_HISTORY = 2000


def rank_topics(topics, states, prior_wrong_topic_ids=()):
    """Rank frozen scoped topics; shared by live selection and historical proxy."""
    known = {topic["id"]: topic for topic in topics}
    parents = {parent for topic_id in prior_wrong_topic_ids if topic_id in known
               for parent in known[topic_id].get("prerequisites", []) if parent in known}
    ranked = []
    for topic in topics:
        identifier = topic["id"]
        state = states.get(identifier, {})
        score = state.get("mastery_score") if state.get("score_validity", "current") == "current" else None
        count = state.get("independent_evidence_count", 0)
        if identifier in parents:
            priority, reason = 0, "investigate_prerequisite"
        elif score is None or count < 3:
            priority, reason = 1, "insufficient_evidence"
        elif score < 0.8 or state.get("status") in {"unstable", "needs_review"}:
            priority, reason = 2, "weak_topic"
        else:
            priority, reason = 3, "topic_coverage"
        ranked.append(((priority, -1 if score is None else score, count, identifier),
                       {"topic_id": identifier, "reason": reason,
                        "evidence_ids": list(state.get("evidence_ids", []))}))
    return [item for _, item in sorted(ranked, key=lambda pair: pair[0])]


def freeze_inputs(topics, evidence, previous_assessments, epochs, state_version, timestamp, *, policy):
    """Freeze scores, exact evidence identifiers and previously exposed families."""
    inputs = {}
    for topic in topics:
        rows = [e for e in evidence if e["topic_id"] == topic["id"]
                and e.get("topic_revision_id") == topic["revision_id"]
                and e.get("epoch", 0) == epochs.get(topic["id"], 0)
                and not e.get("revoked_by_review_id")]
        state = aggregate(topic["id"], topic["revision_id"], rows, {}, state_version, timestamp, policy=policy)
        inputs[topic["id"]] = {field: deepcopy(state[field]) for field in (
            "mastery_score", "score_validity", "status", "independent_evidence_count", "evidence_ids")}
    topic_ids = {topic["id"] for topic in topics}
    families = {e["family_id"] for e in evidence if e.get("family_id") and e["topic_id"] in topic_ids}
    for assessment in previous_assessments:
        if assessment["status"] in {"generating", "failed", "cancelled"}:
            continue
        families.update(q["family_id"] for q in reached_questions(assessment) if q["topic_id"] in topic_ids)
    return {"policy_version": POLICY_VERSION, "selection_inputs": inputs,
            "selection_state_version": state_version, "frozen_at": timestamp,
            "historical_family_ids": sorted(families), "used_family_ids": sorted(families),
            "presented_question_ids": [], "answered_question_ids": [], "current_question_id": None,
            "decisions": [], "hypotheses": [], "completion_reason": None}


def reached_questions(assessment):
    adaptive = assessment["snapshot"].get("adaptive")
    if adaptive is None:
        return assessment["questions"]
    by_id = {q["id"]: q for q in assessment["questions"]}
    return [by_id[qid] for qid in adaptive["presented_question_ids"] if qid in by_id]


def public_question(question):
    return {field: deepcopy(question[field]) for field in ("id", "type", "prompt", "options", "difficulty") if field in question} | {"topic_ids": [question["topic_id"]]}


def _available(assessment):
    adaptive = assessment["snapshot"]["adaptive"]
    used = set(adaptive["used_family_ids"])
    shown = set(adaptive["presented_question_ids"])
    return [q for q in assessment["questions"] if q["id"] not in shown and q["family_id"] not in used]


def exclude_runtime_exposures(assessment, history, timestamp):
    """Reserve unseen pool families against exposures published after freezing.

    Callers hold the owning space lock. Only not-yet-presented candidates can
    become excluded: later overlapping pools never invalidate our first real
    exposure or rewrite the frozen evidence/prior-family snapshot.
    """
    diagnostic = assessment["snapshot"]["adaptive"]
    remaining = {q["family_id"] for q in _available(assessment)}
    used = set(diagnostic["used_family_ids"])
    inspected = 0
    for other in history:
        if not remaining:
            break
        if other["id"] == assessment["id"] or other["status"] in {"generating", "failed", "cancelled"}:
            continue
        inspected += 1
        if inspected > MAX_RUNTIME_HISTORY:
            diagnostic.update(current_question_id=None, completion_reason="exposure_history_limit")
            diagnostic["used_family_ids"] = sorted(used)
            return False
        for question in reached_questions(other):
            family = question["family_id"]
            if family not in remaining:
                continue
            remaining.remove(family)
            used.add(family)
            # Matching exclusions are bounded by this immutable pool (5–10
            # families). The public view returns a count, never future content.
            diagnostic.setdefault("runtime_family_exclusions", []).append({
                "family_id": family, "assessment_id": other["id"],
                "question_id": question["id"], "recorded_at": timestamp,
                "reason": "already_exposed_in_space"})
    diagnostic["used_family_ids"] = sorted(used)
    return True


def select_next(assessment, *, trigger=None, attempt=None):
    """Append one safe public decision without publishing remaining candidates."""
    snapshot, adaptive = assessment["snapshot"], assessment["snapshot"]["adaptive"]
    adaptive["current_question_id"] = None
    if len(adaptive["answered_question_ids"]) >= snapshot["request"]["question_count"]:
        adaptive["completion_reason"] = "target_reached"
        return
    available = _available(assessment)
    topics = {topic["id"]: topic for topic in snapshot["topics"]}
    chosen, reason, hypothesis = None, None, None
    if trigger is not None:
        parents = topics[trigger["topic_id"]].get("prerequisites", [])
        ranked = rank_topics(snapshot["topics"], adaptive["selection_inputs"], [trigger["topic_id"]])
        for candidate in ranked:
            if candidate["topic_id"] not in parents:
                continue
            chosen = next((q for q in available if q["topic_id"] == candidate["topic_id"]), None)
            if chosen is not None:
                hypothesis = {"id": f"hypothesis-{len(adaptive['hypotheses']) + 1}",
                    "dependent_topic_id": trigger["topic_id"], "prerequisite_topic_id": chosen["topic_id"],
                    "trigger_question_id": trigger["id"], "probe_question_id": chosen["id"],
                    "status": "pending", "reason": "requires_independent_verification",
                    "attempt_ids": [attempt["id"]], "evidence_ids": [], "causal_claim": False}
                adaptive["hypotheses"].append(hypothesis)
                reason = "investigate_prerequisite"
                break
        if chosen is None:
            for parent in sorted(parents):
                adaptive["hypotheses"].append({"id": f"hypothesis-{len(adaptive['hypotheses']) + 1}",
                    "dependent_topic_id": trigger["topic_id"], "prerequisite_topic_id": parent,
                    "trigger_question_id": trigger["id"], "probe_question_id": None,
                    "status": "inconclusive", "reason": "no_available_prerequisite_question" if parent in topics else "prerequisite_out_of_scope",
                    "attempt_ids": [attempt["id"]], "evidence_ids": [], "causal_claim": False})
    if chosen is None:
        for candidate in rank_topics(snapshot["topics"], adaptive["selection_inputs"]):
            chosen = next((q for q in available if q["topic_id"] == candidate["topic_id"]), None)
            if chosen is not None:
                reason = candidate["reason"]
                break
    if chosen is None:
        adaptive["completion_reason"] = "no_unseen_question_family"
        return
    adaptive["current_question_id"] = chosen["id"]
    adaptive["presented_question_ids"].append(chosen["id"])
    adaptive["used_family_ids"].append(chosen["family_id"])
    decision = {"step": len(adaptive["decisions"]) + 1, "question_id": chosen["id"],
                "topic_id": chosen["topic_id"], "reason": reason,
                "evidence_ids": list(adaptive["selection_inputs"][chosen["topic_id"]]["evidence_ids"]),
                "attempt_ids": [attempt["id"]] if attempt else []}
    if hypothesis:
        decision["hypothesis_id"] = hypothesis["id"]
    adaptive["decisions"].append(decision)


def update_hypotheses(assessment, observations):
    """Revalidate projections after assistance, reset, stale knowledge or review."""
    adaptive = assessment["snapshot"]["adaptive"]
    for hypothesis in adaptive["hypotheses"]:
        trigger = observations.get(hypothesis["trigger_question_id"], {})
        probe = observations.get(hypothesis["probe_question_id"], {})
        hypothesis["attempt_ids"] = [item["attempt_id"] for item in (trigger, probe) if item.get("attempt_id")]
        hypothesis["evidence_ids"] = [item["evidence_id"] for item in (trigger, probe) if item.get("evidence_id")]
        if not trigger.get("independent") or trigger.get("score") != 0:
            hypothesis.update(status="inconclusive", reason="trigger_not_independent_incorrect")
        elif hypothesis["probe_question_id"] is None:
            continue
        elif not probe.get("attempt_id"):
            hypothesis.update(status="inconclusive" if assessment["status"] == "completed" else "pending",
                              reason="probe_unanswered" if assessment["status"] == "completed" else "requires_independent_verification")
        elif not probe.get("independent"):
            hypothesis.update(status="inconclusive", reason="probe_not_independent_verified")
        else:
            hypothesis.update(status="supported" if probe["score"] == 0 else "not_supported",
                              reason="independent_prerequisite_observation")


def diagnostic_view(assessment):
    adaptive = assessment["snapshot"]["adaptive"]
    current_id = adaptive["current_question_id"] if assessment["status"] in {"ready", "in_progress"} else None
    question = next((q for q in reached_questions(assessment) if q["id"] == current_id), None)
    return {"assessment_id": assessment["id"], "adaptive": True, "policy_version": adaptive["policy_version"],
            "status": assessment["status"], "current_question": public_question(question) if question else None,
            "progress": {"answered": len(adaptive["answered_question_ids"]),
                         "presented": len(adaptive["presented_question_ids"]),
                         "target": assessment["snapshot"]["request"]["question_count"]},
            "decisions": deepcopy(adaptive["decisions"]), "hypotheses": deepcopy(adaptive["hypotheses"]),
            "runtime_excluded_family_count": len(adaptive.get("runtime_family_exclusions", [])),
            "completion_reason": adaptive["completion_reason"],
            "limitations": ["Rule priorities are not calibrated probabilities.",
                "Prerequisite observations do not establish the cause of a dependent error.",
                "Only the validated frozen pool is used; equivalent paraphrases are not semantically deduplicated."]}
