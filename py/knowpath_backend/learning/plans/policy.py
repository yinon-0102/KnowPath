"""Deterministic, versioned study-task selection and bounded daily scheduling."""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.plans.ordering import reorder_task_nodes


@dataclass(frozen=True)
class PlannerPolicy:
    version: str = "planner-v2"
    learn_minutes: int = 10
    practice_minutes: int = 15
    review_minutes: int = 10
    review_intervals: tuple[int, ...] = (1, 3, 7)


def conflict(message, constraints, adjustments):
    raise DomainConflict("PLAN_CONSTRAINT_UNSATISFIABLE", message,
                         {"conflicts": constraints, "adjustable_constraints": adjustments})


def topic_fingerprint(topic, state, config, policy):
    inputs = {"topic": {k: topic.get(k) for k in ("id", "source_refs", "prerequisites", "status", "learning_revision_id")},
              "state": state, "include_review": config["include_review"], "policy": policy.version}
    return sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def build_tasks(topics, states, config, space, old_tasks, timestamp, policy, *, active_node_ids=None, source_positions=None):
    moment = datetime.fromisoformat(timestamp)
    today = moment.date()
    old_by_topic = {t["topic_ids"][0]: t for t in old_tasks if not t.get("context", {}).get("historical")}
    tasks = []
    archived = set()
    selected = {t["id"] for t in topics}
    prerequisites = {p for t in topics for p in t.get("prerequisites", [])}
    for topic in topics:
        state = states.get(topic["id"], {})
        fingerprint = topic_fingerprint(topic, state, config, policy)
        previous = old_by_topic.get(topic["id"])
        status = state.get("status", "unseen")
        due_at = state.get("review_due_at") or state.get("next_review_at")
        due = state.get("review_due", due_at is not None and datetime.fromisoformat(due_at).date() <= today)
        invalidated = bool(previous and previous.get("context", {}).get("knowledge_invalidated"))
        kind, estimate, reason = None, None, None
        if state.get("score_validity") == "stale" or invalidated:
            kind, estimate = "diagnostic", policy.learn_minutes
            reason = "知识版本已更新，需在新快照下重新验证；历史完成记录不作为当前掌握证据"
        elif status in {"mastered", "needs_review"}:
            if config["include_review"] and (status == "needs_review" or due):
                kind, estimate = "review", policy.review_minutes
                reason = f"复习到期 {due_at or today.isoformat()}；使用共享 review-v2 间隔策略"
        elif status == "unstable" or state.get("error_tags") or (state.get("mastery_score") is not None and state["mastery_score"] < 0.6):
            kind, estimate = "targeted_practice", policy.practice_minutes
            reason = f"掌握度 {state.get('mastery_score')}；需练习的错误类型：{', '.join(state.get('error_tags') or ['掌握不稳定'])}"
        elif topic["id"] in prerequisites and status == "unseen":
            kind, estimate = "diagnostic", policy.learn_minutes
            reason = "后续任务依赖该前置知识点，先检查当前掌握情况"
        else:
            kind, estimate = "learn", policy.learn_minutes
            reason = "当前范围内尚未形成充分独立证据，安排来源学习"
        # A terminal choice closes one observation/review cycle, not the topic
        # forever. Presentation preferences must not create a new cycle.
        cycle_state = {k: v for k, v in state.items() if k != "review_due"}
        cycle = sha256(json.dumps([kind, topic_fingerprint(topic, cycle_state, {"include_review": True}, policy)],
                                  sort_keys=True).encode()).hexdigest()
        elapsed_deferral = bool(previous and previous["status"] == "deferred"
            and previous.get("defer_until") and datetime.fromisoformat(previous["defer_until"]) <= moment)
        future_deferral = bool(previous and previous["status"] == "deferred" and not elapsed_deferral)
        same_cycle = False
        if previous and not invalidated:
            old_context = previous.get("context", {})
            same_cycle = old_context.get("cycle_fingerprint") == cycle
            # Legacy tasks have no cycle signature; use their original input.
            if "cycle_fingerprint" not in old_context:
                legacy_state = {k: v for k, v in state.items() if k != "review_due"}
                legacy_policy = replace(policy, version=old_context.get("policy_version", "planner-v1"))
                legacy_fingerprints = {topic_fingerprint(topic, legacy_state,
                    {"include_review": value}, legacy_policy) for value in (True, False)}
                same_cycle = (old_context.get("input_fingerprint") in {fingerprint, *legacy_fingerprints}
                              and previous["kind"] == kind)
            terminal = previous["status"] in {"completed", "skipped"}
            unchanged = old_context.get("input_fingerprint") == fingerprint and previous["kind"] == kind
            if ((terminal and (same_cycle or kind is None)) or future_deferral
                    or (not terminal and not elapsed_deferral and unchanged)):
                task = deepcopy(previous)
                task["id"] = str(uuid4())
                task["context"]["origin_task_id"] = previous["id"]
                task["context"].setdefault("node_id", previous["id"])
                task["context"].setdefault("sequence", old_context.get("position", 0) + 1)
                tasks.append(task)
                continue
        if previous and (previous["status"] in {"completed", "skipped"} or invalidated):
            archived.add(previous["id"])
        not_required = kind is None and config.get('schedule_mode') == 'budget_rolling'
        if kind is None:
            if not not_required:
                continue
            kind, estimate, reason = 'learn', policy.learn_minutes, '当前已掌握且尚未到复习期，保留路线但无需安排'
        context = {"input_fingerprint": fingerprint, "cycle_fingerprint": cycle, "policy_version": policy.version,
                   "prerequisites": deepcopy(topic.get("prerequisites", []))}
        if not_required:
            context['not_required'] = True
        if previous:
            context["origin_task_id"] = previous["id"]
        if elapsed_deferral:
            context["previous_defer_until"] = previous["defer_until"]
        if previous and not invalidated and same_cycle and previous["kind"] == kind:
            if not previous.get("context", {}).get("historical"):
                context.update({key: deepcopy(value) for key, value in previous.get("context", {}).items()
                                if key in {"node_id", "sequence", "title", "topics", "source_refs"}})
                context.setdefault("node_id", previous["id"])
                context.setdefault("sequence", previous.get("context", {}).get("position", 0) + 1)
        tasks.append({"id": str(uuid4()), "topic_ids": [topic["id"]], "kind": kind,
                      "status": "deferred" if future_deferral else "pending", "estimated_minutes": estimate,
                      "reason": reason, "note": previous.get("note") if previous and (same_cycle or elapsed_deferral or future_deferral) else None,
                      "defer_until": previous["defer_until"] if future_deferral else None, "context": context})
    # Historical tasks remain traceable even when their topics leave the scope.
    for previous in old_tasks:
        if (previous["topic_ids"][0] not in selected or previous.get("context", {}).get("historical")
                or previous.get("context", {}).get("knowledge_invalidated") or previous["id"] in archived):
            task = deepcopy(previous)
            task["id"] = str(uuid4())
            task["context"].update(origin_task_id=previous["id"], historical=True)
            task["context"].setdefault("node_id", previous["id"])
            task["context"].setdefault("sequence", previous.get("context", {}).get("position", 0) + 1)
            tasks.append(task)
    fixed_ids = set()
    if config.get("order_mode") == "custom" and config.get("custom_node_order"):
        tasks, fixed_ids = reorder_task_nodes(tasks, topics, states, config,
                                             active_node_ids=active_node_ids, source_positions=source_positions)
        config["custom_node_order"] = [t.get("context", {}).get("node_id", t["id"])
                                       for t in tasks if not t.get("context", {}).get("historical")]
    active = [t for t in tasks if t["status"] not in {"completed", "skipped", "deferred"} and not t["context"].get("historical")]
    by_topic = {t["topic_ids"][0]: t for t in tasks if not t["context"].get("historical")}
    # A deferred unmet prerequisite has no executable slot in this plan. Reject
    # instead of silently scheduling any transitive dependent ahead of it.
    def deferred_ancestor(topic_id, visited):
        if topic_id in visited:
            return None
        visited.add(topic_id)
        candidate = by_topic.get(topic_id)
        if not candidate or states.get(topic_id, {}).get("status") == "mastered":
            return None
        if candidate["status"] == "deferred":
            return topic_id
        for parent in candidate["context"].get("prerequisites", []):
            blocked = deferred_ancestor(parent, visited)
            if blocked:
                return blocked
        return None

    for task in active:
        blocked = deferred_ancestor(task["topic_ids"][0], set())
        if blocked:
            conflict("任务依赖的前置知识已延期，无法在当前计划中安排",
                     ["deferred_prerequisite", blocked], ["完成前置学习后重新规划", "缩小依赖该前置的范围"])
    return schedule_tasks(tasks, config, space, today, fixed_task_ids=fixed_ids)


def schedule_tasks(tasks, config, space, today, *, fixed_task_ids=None):
    """Allocate bounded daily slots; reorder reserves frozen executed slots."""
    if config.get('schedule_mode') == 'budget_rolling':
        from .rolling import allocate_ordered_tasks
        for position, task in enumerate(tasks):
            context = task.setdefault('context', {})
            context['position'] = position
            context.setdefault('node_id', task['id'])
            context.setdefault('sequence', position + 1)
            task['node_id'], task['sequence'] = context['node_id'], context['sequence']
        tasks[:] = allocate_ordered_tasks(tasks, config, space, today, protected_ids=fixed_task_ids or ())['tasks']
        return tasks
    fixed = set(fixed_task_ids or [])
    active = [t for t in tasks if t["status"] not in {"completed", "skipped", "deferred"}
              and not t.get("context", {}).get("historical")]
    frozen = [t for t in tasks if t["id"] in fixed and not t.get("context", {}).get("historical")
              and t["status"] != "deferred" and t.get("context", {}).get("session_index") is not None
              and (t["status"] not in {"completed", "skipped"}
                   or t.get("context", {}).get("scheduled_date", today.isoformat()) >= today.isoformat())]
    counted = {t["id"]: t for t in active + frozen}
    total = sum(t["estimated_minutes"] for t in counted.values())
    weekly = space.get("weekly_minutes")
    if weekly is not None and total > weekly:
        conflict("实际任务时长超出本周预算", ["weekly_minutes"], ["增加 weekly_minutes", "缩小学习范围"])
    reserved = {}
    for task in frozen:
        index = task["context"]["session_index"]
        reserved[index] = reserved.get(index, 0) + task["estimated_minutes"]
    capacity = config["minutes_per_session"]
    if any(index >= config["session_count"] or used > capacity for index, used in reserved.items()):
        conflict("固定任务占用超出会话容量", ["session_count", "minutes_per_session"], ["保持原顺序", "缩小学习范围"])
    slot, used = 0, dict(reserved)
    deadline = date.fromisoformat(space["target_date"]) if space.get("target_date") else None
    active_ids = {t["id"] for t in active}
    frozen_ids = {t["id"] for t in frozen}
    for position, task in enumerate(tasks):
        context = task.setdefault("context", {})
        context["position"] = position
        context.setdefault("node_id", task["id"])
        context.setdefault("sequence", position + 1)
        task["node_id"], task["sequence"] = context["node_id"], context["sequence"]
        if task["id"] in frozen_ids:
            index = context["session_index"]
            if slot > index:
                conflict("重排无法保留已执行或活动会话的日程", ["fixed_schedule", "session_count"], ["保持原顺序", "调整待学任务顺序"])
            slot = max(slot, index)
            continue
        if task["id"] not in active_ids:
            if task["status"] == "deferred" and task["id"] not in fixed:
                context.pop("session_index", None)
                context.pop("scheduled_date", None)
            continue
        minutes = task["estimated_minutes"]
        while slot < config["session_count"] and used.get(slot, 0) + minutes > capacity:
            slot += 1
        if slot >= config["session_count"] or minutes > capacity:
            conflict("任务无法装入所请求的会话容量", ["session_count", "minutes_per_session"], ["增加会话数或时长", "缩小学习范围"])
        scheduled = today + timedelta(days=slot)
        if deadline and scheduled > deadline:
            conflict("每天一次的学习安排超过目标日期", ["target_date"], ["延后 target_date", "增加 minutes_per_session", "缩小学习范围"])
        context.update(session_index=slot, scheduled_date=scheduled.isoformat())
        used[slot] = used.get(slot, 0) + minutes
    return tasks
