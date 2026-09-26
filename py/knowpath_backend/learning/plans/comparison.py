"""Deterministic dependency-closure budget previews with no business writes.

This is a bounded coverage heuristic. Task estimates are planning defaults, not
predicted learning gains; scheduling a prerequisite does not prove mastery.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from pydantic import ValidationError

from knowpath_backend.learning.assessments.service import revision
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.plans.comparison_schemas import ComparePlans
from knowpath_backend.learning.plans.policy import PlannerPolicy


MAX_TOPICS = 512
MAX_RELATIONS = 4096
STRATEGY_VERSION = "budget-closure-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _limit(message):
    raise DomainConflict("COMPARISON_LIMIT_EXCEEDED", message,
                         {"max_topics": MAX_TOPICS, "max_relations": MAX_RELATIONS})


def _validate_graph(snapshot):
    """Reject malformed immutable input before the shared graph projection."""
    def invalid():
        raise DomainConflict("INVALID_GRAPH_SNAPSHOT", "绑定的知识图谱快照结构无效，需审核后重新发布")
    if not isinstance(snapshot, dict):
        invalid()
    for field in ("nodes", "relations", "sources"):
        rows = snapshot.get(field, [])
        if not isinstance(rows, list):
            invalid()
        if len(rows) > (MAX_TOPICS if field == "nodes" else MAX_RELATIONS):
            _limit("绑定图谱超出有界比较规模，请缩小资料范围")
        identifiers = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in identifiers:
                invalid()
            identifiers.add(row["id"])
            if field == "nodes" and not isinstance(row.get("name"), str):
                invalid()
            if field == "relations" and not all(isinstance(row.get(key), str) for key in ("from_id", "to_id", "type")):
                invalid()
            if field != "sources":
                refs = row.get("source_refs", [])
                if not isinstance(refs, list) or not all(isinstance(ref, dict) and isinstance(ref.get("chunk_id"), str) for ref in refs):
                    invalid()


def _cycle_nodes(dependencies):
    """Iterative DFS avoids recursion limits on corrupt/deep graph snapshots."""
    finished, cyclic = set(), set()
    for root in sorted(dependencies):
        if root in finished:
            continue
        path, indices = [root], {root: 0}
        stack = [(root, iter(sorted(dependencies[root])))]
        while stack:
            node, parents = stack[-1]
            parent = next(parents, None)
            if parent is None:
                finished.add(node)
                stack.pop()
                indices.pop(node)
                path.pop()
            elif parent in indices:
                cyclic.update(path[indices[parent]:])
            elif parent in dependencies and parent not in finished:
                indices[parent] = len(path)
                path.append(parent)
                stack.append((parent, iter(sorted(dependencies[parent]))))
    return cyclic


def _is_due(state, today):
    # Shared review-v2 projects an exact-time boolean. A date-only fallback
    # must never override its explicit False for a review due later today.
    if "review_due" in state:
        return bool(state["review_due"])
    value = state.get("review_due_at") or state.get("next_review_at")
    try:
        return bool(state.get("review_due") or state.get("status") == "needs_review"
                    or value and datetime.fromisoformat(value).date() <= today)
    except (ValueError, TypeError):
        return False


class PlanComparisonService:
    def __init__(self, plans):
        self.plans = plans
        self.repository = plans.repository
        self.spaces = plans.spaces
        self.assessments = plans.assessments
        self.policy = PlannerPolicy()

    def compare(self, space_id, payload, idempotency_key=None):
        try:
            request = ComparePlans.model_validate(payload).model_dump()
        except ValidationError as exc:
            raise DomainConflict("INVALID_REQUEST", "预算比较参数无效",
                {"errors": [{k: v for k, v in error.items() if k not in {"ctx", "input"}}
                            for error in exc.errors(include_url=False)]}) from None

        def preview():
            # The normal command unit of work holds the owning space row lock,
            # so states, resets, profile and bindings are frozen consistently.
            space = self.spaces.repository.get(space_id)
            timestamp = now()
            today = datetime.fromisoformat(timestamp).astimezone(timezone.utc).date()
            topics = self._topics(space)
            states = copy.deepcopy(self.plans._planning_states(space_id, as_of=timestamp))
            epochs = self.assessments._epochs(space_id)
            for topic in topics.values():
                state = states.get(topic["id"], {})
                if state and (state.get("topic_revision_id") != revision(topic)
                              or state.get("epoch", 0) != epochs.get(topic["id"], 0)):
                    state.update(score_validity="stale", status="unseen", mastery_score=None,
                                 error_tags=[], review_due=False, review_due_at=None, next_review_at=None)
            selected, excluded = self._scope(space, topics)
            state_snapshot = {identifier: states.get(identifier, {}) for identifier in sorted(topics)}
            horizon = request["horizon_days"]
            if space.get("target_date"):
                horizon = min(horizon, max(0, (date.fromisoformat(space["target_date"]) - today).days + 1))
            metadata = {"scope_version": space["scope_version"], "profile_version": space["profile_version"],
                "state_version": space["state_version"], "bindings": copy.deepcopy(space["bindings"]),
                "topic_ids": sorted(selected), "excluded_topic_ids": sorted(excluded),
                "weekly_minutes": space.get("weekly_minutes"), "target_date": space.get("target_date"),
                "start_date": today.isoformat(), "effective_horizon_days": horizon}
            metadata["hash"] = _digest({"space_id": space_id, "metadata": metadata,
                "topics": topics, "states": state_snapshot, "epochs": epochs,
                "strategy_version": STRATEGY_VERSION, "duration_policy_version": self.policy.version})
            scenarios = [self._scenario(topics, states, selected, excluded, space, today, horizon,
                                       request["horizon_days"], minutes) for minutes in request["budgets_minutes_per_day"]]
            return {"space_id": space_id, "strategy_version": STRATEGY_VERSION, "created_at": timestamp,
                "snapshot": metadata, "scenarios": scenarios, "assumptions": [
                    "Deterministic dependency-closure heuristic; not a globally optimal solution.",
                    "Coverage and priority are descriptive proxies, not predicted or causal learning gains.",
                    "UTC dates; deadline inclusive; weekly caps reset on Monday (ISO calendar weeks).",
                    "Each topic receives at most one indivisible task; default estimates: learn/diagnostic/review 10 minutes, targeted practice 15 minutes.",
                    "Due topics have priority 4, weak topics 3, unknown topics 2, other learning topics 1; ties use dependency-closure duration and topic ID.",
                    "A scheduled prerequisite is an ordering assumption, not proof of mastery; current mastered prerequisites require no extra prerequisite task.",
                    "Budgets describe standalone preview capacity; existing plans are neither replaced nor charged again.",
                    "Missing learner evidence remains unknown; the preview changes no tasks, plans, learner state or evidence."]}

        return self.assessments._execute("plan.compare", space_id, request, idempotency_key, preview)

    def _topics(self, space):
        """Use pinned sources and preserve invalid cycles as explicit blockers.

        The normal projection drops cyclic edges for automatic scope expansion.
        Previews must instead explain why a dependent cannot be scheduled, so
        read the same immutable graph and retain grounded confirmed cycles.
        """
        raw_graphs, total_nodes, total_relations = [], 0, 0
        for binding in space["bindings"]:
            if binding.get("graph_revision_id"):
                graph = self.spaces.graphs.repository.get_record("graph_revisions", binding["graph_revision_id"], lock=False)
                publication = graph.get("publication")
                snapshot = publication.get("snapshot") if isinstance(publication, dict) else None
                _validate_graph(snapshot)
                total_nodes += len(snapshot.get("nodes", []))
                total_relations += len(snapshot.get("relations", []))
                if total_nodes > MAX_TOPICS or total_relations > MAX_RELATIONS:
                    _limit("绑定图谱超出有界比较规模，请缩小资料范围")
                raw_graphs.append(snapshot)
        bound = self.spaces.bound_topics(space)
        if len({t.get("canonical_topic_id", t["id"]) for t in bound}) > MAX_TOPICS:
            _limit("绑定知识点超出有界比较规模，请缩小资料范围")
        topics = {t["id"]: copy.deepcopy(t) for t in bound}
        # Reconstruct only grounded, active, confirmed edges. Unsupported or
        # pending recommendations never become hard prerequisite constraints.
        authoritative = defaultdict(set)
        graph_nodes = set()
        for snapshot in raw_graphs:
            nodes = {t["id"]: t for t in snapshot.get("nodes", [])}
            graph_nodes.update(nodes)
            sources = {s["id"]: s for s in snapshot.get("sources", [])}
            for edge in snapshot.get("relations", []):
                if (edge.get("type") != "prerequisite_of" or edge.get("status") != "active"
                        or edge.get("confirmed") is False):
                    continue
                parent, child = edge.get("from_id"), edge.get("to_id")
                refs = edge.get("source_refs") or []
                grounded = refs and all(ref.get("chunk_id") in sources and
                    all(ref.get(field) == sources[ref["chunk_id"]].get(field)
                        for field in ("material_id", "material_version_id")) for ref in refs)
                if not grounded or child not in nodes or nodes[child].get("status", "active") != "active":
                    continue
                if isinstance(parent, str) and parent:
                    authoritative[child].add(parent)
        for topic in topics.values():
            canonical = topic.get("canonical_topic_id", topic["id"])
            if canonical in graph_nodes:
                topic["prerequisites"] = sorted(authoritative[canonical])
            else:
                topic["prerequisites"] = []
        return topics

    @staticmethod
    def _scope(space, topics):
        canonical = lambda identifier: topics.get(identifier, {}).get("canonical_topic_id", identifier)
        excluded = {canonical(identifier) for identifier in space.get("excluded_topic_ids", [])}
        candidates = space.get("topic_ids") or sorted(topics)
        selected = {}
        for identifier in sorted(candidates):
            if canonical(identifier) not in excluded:
                selected.setdefault(canonical(identifier), identifier)
        return set(selected.values()), excluded

    def _scenario(self, topics, states, selected, excluded, space, today, horizon, requested_horizon, minutes):
        by_canonical = {t.get("canonical_topic_id", identifier): t.get("canonical_topic_id", identifier)
                        for identifier, t in topics.items()}
        by_canonical.update({topics.get(i, {}).get("canonical_topic_id", i): i for i in selected})
        dependencies = {i: sorted({by_canonical.get(p, p) for p in topics.get(i, {}).get("prerequisites", [])}) for i in selected}
        satisfied = {i for i, state in states.items() if state.get("status") == "mastered"
                     and state.get("score_validity") == "current" and state.get("mastery_score") is not None
                     and state.get("independent_evidence_count", 0) > 0 and i in topics
                     and topics[i].get("status", "active") == "active"}
        blockers = {}
        for identifier in sorted(selected):
            reasons = []
            if identifier not in topics or topics[identifier].get("status", "active") != "active":
                reasons.append("inactive_or_missing_topic")
            for parent in dependencies[identifier]:
                canonical = topics.get(parent, {}).get("canonical_topic_id", parent)
                # An excluded/out-of-scope prerequisite stays an explicit scope
                # blocker even if its old learner state says mastered.
                if canonical in excluded:
                    reasons.append("excluded_prerequisite")
                elif parent not in topics or topics[parent].get("status", "active") != "active":
                    reasons.append("missing_prerequisite")
                elif parent not in selected:
                    reasons.append("out_of_scope_prerequisite")
            if reasons:
                blockers[identifier] = sorted(set(reasons))
        cyclic = _cycle_nodes({i: set(dependencies[i]) & selected for i in selected})
        for identifier in cyclic:
            blockers.setdefault(identifier, []).append("prerequisite_cycle")
        # Propagate malformed/missing prerequisites without blocking unrelated goals.
        changed = True
        while changed:
            changed = False
            for identifier in sorted(selected - blockers.keys()):
                if any(parent in blockers for parent in dependencies[identifier]):
                    blockers[identifier] = ["blocked_prerequisite"]
                    changed = True
        tasks = {}
        for identifier in sorted(selected - blockers.keys()):
            state = states.get(identifier, {})
            due = _is_due(state, today)
            weak = state.get("status") == "unstable" or bool(state.get("error_tags"))
            score = state.get("mastery_score")
            weak = weak or isinstance(score, (int, float)) and score < 0.6
            unknown = not state or state.get("score_validity") in {None, "unverified", "stale"} or score is None
            if identifier in satisfied and not due:
                continue
            if due:
                kind, duration, priority, reason = "review", self.policy.review_minutes, 4, "review_due"
            elif weak:
                kind, duration, priority, reason = "targeted_practice", self.policy.practice_minutes, 3, "weak_state"
            else:
                kind, duration, priority, reason = "learn", self.policy.learn_minutes, 2 if unknown else 1, "unknown_state" if unknown else "learning_state"
            if state.get("score_validity") == "stale":
                kind, reason = "diagnostic", "stale_state"
            tasks[identifier] = {"topic_id": identifier, "name": topics[identifier]["name"],
                "kind": kind, "estimated_minutes": duration, "prerequisite_ids": dependencies[identifier],
                "reason_codes": [reason], "priority": priority, "unknown": unknown, "weak": bool(weak), "due": due}
        closures = {}
        for identifier in sorted(tasks):
            # A valid mastered prerequisite needs no dependency budget, even if
            # it also has its own optional due review task in this comparison.
            order, seen, stack = [], set(), [(identifier, False)]
            while stack:
                node, expanded = stack.pop()
                if expanded:
                    order.append(node)
                elif node not in seen and node in tasks:
                    seen.add(node)
                    stack.append((node, True))
                    stack.extend((p, False) for p in reversed(dependencies[node]) if p not in satisfied)
            closures[identifier] = order
        ranking = sorted(tasks, key=lambda i: (-tasks[i]["priority"],
                         sum(tasks[p]["estimated_minutes"] for p in closures[i]), i))
        daily = [0] * horizon
        week_keys = [(today + timedelta(days=d) - timedelta(days=(today + timedelta(days=d)).weekday())).isoformat() for d in range(horizon)]
        weekly, placed, failures = defaultdict(int), {}, {}
        weekly_cap = space.get("weekly_minutes")
        for identifier in ranking:
            if identifier in placed:
                continue
            trial_days, trial_weeks, trial_placed = daily.copy(), weekly.copy(), dict(placed)
            failed = []
            for node in closures[identifier]:
                if node in trial_placed:
                    continue
                task = tasks[node]
                duration = task["estimated_minutes"]
                earliest = max((trial_placed[p]["day_index"] for p in dependencies[node] if p in trial_placed and p not in satisfied), default=0)
                slot = next((d for d in range(earliest, horizon) if trial_days[d] + duration <= minutes
                             and (weekly_cap is None or trial_weeks[week_keys[d]] + duration <= weekly_cap)), None)
                if slot is None:
                    if duration > minutes:
                        failed.append("daily_budget")
                    if weekly_cap is not None and (duration > weekly_cap or horizon and
                            all(trial_weeks[w] + duration > weekly_cap for w in set(week_keys[earliest:]))):
                        failed.append("weekly_budget")
                    if horizon < requested_horizon:
                        failed.append("deadline")
                    if not failed:
                        failed.append("horizon_capacity")
                    if node != identifier:
                        failed.append("prerequisite_budget")
                    break
                trial_days[slot] += duration
                trial_weeks[week_keys[slot]] += duration
                trial_placed[node] = {**task, "day_index": slot, "scheduled_date": (today + timedelta(days=slot)).isoformat(),
                                      "position": len(trial_placed)}
            if failed:
                failures[identifier] = failed
            else:
                daily, weekly, placed = trial_days, trial_weeks, trial_placed
        schedule = sorted(placed.values(), key=lambda t: (t["day_index"], t["position"]))
        summary = {"eligible_topic_count": len(tasks), "selected_topic_count": len(schedule),
            "coverage_fraction": len(schedule) / len(tasks) if tasks else None,
            "estimated_minutes": sum(t["estimated_minutes"] for t in schedule),
            "weak_topics_covered": sum(t["weak"] for t in schedule), "due_topics_covered": sum(t["due"] for t in schedule),
            "unknown_topics_covered": sum(t["unknown"] for t in schedule)}
        for position, item in enumerate(schedule):
            item["position"] = position
            for key in ("priority", "unknown", "weak", "due"):
                item.pop(key)
        def excluded_item(identifier, reasons):
            return {"topic_id": identifier, "name": topics.get(identifier, {}).get("name", identifier),
                    "reason_codes": sorted(set(reasons)), "prerequisite_ids": dependencies[identifier]}
        return {"minutes_per_day": minutes, "schedule": schedule, "summary": summary,
            "blocked": [excluded_item(i, blockers[i]) for i in sorted(blockers)],
            "deferred": [excluded_item(i, failures.get(i, ["horizon_capacity"])) for i in sorted(tasks.keys() - placed.keys())],
            "already_satisfied_topic_ids": sorted((satisfied & selected) - blockers.keys()),
            "daily_totals": [{"date": (today + timedelta(days=d)).isoformat(), "minutes": value} for d, value in enumerate(daily)],
            "weekly_totals": [{"week_start": week, "minutes": weekly[week]} for week in sorted(set(week_keys))]}
