"""Deterministic, versioned study-task selection and bounded daily scheduling."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4

from .errors import DomainConflict


@dataclass(frozen=True)
class PlannerPolicy:
    version: str = "planner-v1"
    learn_minutes: int = 10
    practice_minutes: int = 15
    review_minutes: int = 10
    review_intervals: tuple[int, ...] = (1, 3, 7)


def conflict(message, constraints, adjustments):
    raise DomainConflict("PLAN_CONSTRAINT_UNSATISFIABLE", message,
                         {"conflicts": constraints, "adjustable_constraints": adjustments})


def topic_fingerprint(topic, state, config, policy):
    inputs = {"topic": {k: topic.get(k) for k in ("id", "source_refs", "prerequisites", "status")},
              "state": state, "include_review": config["include_review"], "policy": policy.version}
    return sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def build_tasks(topics, states, config, space, old_tasks, timestamp, policy):
    today = datetime.fromisoformat(timestamp).date()
    old_by_topic = {t["topic_ids"][0]: t for t in old_tasks if not t.get("context", {}).get("historical")}
    tasks = []
    selected = {t["id"] for t in topics}
    prerequisites = {p for t in topics for p in t.get("prerequisites", [])}
    for topic in topics:
        state = states.get(topic["id"], {})
        fingerprint = topic_fingerprint(topic, state, config, policy)
        previous = old_by_topic.get(topic["id"])
        # Copy-on-write revisions preserve task history and explicit user choices.
        if previous and (previous["status"] in {"completed", "skipped"} or
                         previous.get("context", {}).get("input_fingerprint") == fingerprint):
            task = deepcopy(previous)
            task["id"] = str(uuid4())
            task["context"]["origin_task_id"] = previous["id"]
            tasks.append(task)
            continue
        status = state.get("status", "unseen")
        due_at = state.get("review_due_at") or state.get("next_review_at")
        due = due_at is not None and datetime.fromisoformat(due_at).date() <= today
        if status in {"mastered", "needs_review"}:
            if not config["include_review"] or (status == "mastered" and not due):
                continue
            kind, estimate = "review", policy.review_minutes
            reason = f"复习到期 {due_at or today.isoformat()}；使用 {policy.version} 间隔策略"
        elif status == "unstable" or state.get("error_tags") or (state.get("mastery_score") is not None and state["mastery_score"] < 0.6):
            kind, estimate = "targeted_practice", policy.practice_minutes
            reason = f"掌握度 {state.get('mastery_score')}；需练习的错误类型：{', '.join(state.get('error_tags') or ['掌握不稳定'])}"
        elif topic["id"] in prerequisites and status == "unseen":
            kind, estimate = "diagnostic", policy.learn_minutes
            reason = "后续任务依赖该前置知识点，先检查当前掌握情况"
        else:
            kind, estimate = "learn", policy.learn_minutes
            reason = "当前范围内尚未形成充分独立证据，安排来源学习"
        context = {"input_fingerprint": fingerprint, "policy_version": policy.version,
                   "prerequisites": deepcopy(topic.get("prerequisites", []))}
        if previous:
            context["origin_task_id"] = previous["id"]
        tasks.append({"id": str(uuid4()), "topic_ids": [topic["id"]], "kind": kind,
                      "status": "pending", "estimated_minutes": min(estimate, config["minutes_per_session"]),
                      "reason": reason, "note": None, "defer_until": None, "context": context})
    # Historical tasks remain traceable even when their topics leave the scope.
    for previous in old_tasks:
        if previous["topic_ids"][0] not in selected or previous.get("context", {}).get("historical"):
            task = deepcopy(previous)
            task["id"] = str(uuid4())
            task["context"].update(origin_task_id=previous["id"], historical=True)
            tasks.append(task)
    active = [t for t in tasks if t["status"] not in {"completed", "skipped", "deferred"} and not t["context"].get("historical")]
    total = sum(t["estimated_minutes"] for t in active)
    weekly = space.get("weekly_minutes")
    if weekly is not None and total > weekly:
        conflict("实际任务时长超出本周预算", ["weekly_minutes"], ["增加 weekly_minutes", "缩小学习范围"])
    slot, used = 0, 0
    deadline = date.fromisoformat(space["target_date"]) if space.get("target_date") else None
    for position, task in enumerate(tasks):
        task["context"]["position"] = position
        if task not in active:
            continue
        minutes = task["estimated_minutes"]
        if used + minutes > config["minutes_per_session"]:
            slot, used = slot + 1, 0
        if slot >= config["session_count"] or minutes > config["minutes_per_session"]:
            conflict("任务无法装入所请求的会话容量", ["session_count", "minutes_per_session"],
                     ["增加会话数或时长", "缩小学习范围"])
        scheduled = today + timedelta(days=slot)
        if deadline and scheduled > deadline:
            conflict("每天一次的学习安排超过目标日期", ["target_date"], ["延后 target_date", "增加 minutes_per_session", "缩小学习范围"])
        task["context"].update(session_index=slot, scheduled_date=scheduled.isoformat())
        used += minutes
    return tasks
