"""Durable study-plan and learning-session commands."""
from __future__ import annotations

import copy
import json
from hashlib import sha256
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4

from sqlalchemy import select

from .db import StudyPlanRow, StudyTaskRow, SessionRow, SessionEventRow
from .errors import DomainConflict, DomainNotFound
from .planner_policy import PlannerPolicy, build_tasks, conflict


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None) if value else None


def iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.replace(tzinfo=timezone.utc).isoformat()


class PlanSessionService:
    EVENT_TYPES = {"open_material", "request_explanation", "request_hint", "pause", "resume"}
    TASK_STATUSES = {"pending", "in_progress", "completed", "skipped", "deferred"}

    def __init__(self, repository, spaces, runs, assessments, memory_plans=None, memory_sessions=None):
        self.repository = repository
        self.spaces = spaces
        self.runs = runs
        self.assessments = assessments
        self.memory_plans = memory_plans if memory_plans is not None else {}
        self.memory_sessions = memory_sessions if memory_sessions is not None else {}
        self.uow = getattr(repository, "unit_of_work", None)
        self.policy = PlannerPolicy()

    @property
    def sql(self):
        return self.uow is not None

    def _fingerprint(self, operation, identifier, payload):
        return sha256(json.dumps([operation, identifier, payload], sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()).hexdigest()

    def _tx(self):
        return self.repository.transaction()

    def _load_plan_memory(self, plan_id):
        return self.repository.materials.assessment_data["plans"].get(plan_id)

    def _load_session_memory(self, session_id):
        return self.repository.materials.assessment_data["sessions"].get(session_id)

    def active_session_id(self, space_id):
        if self.sql:
            with self.uow.session() as session:
                return session.scalar(select(SessionRow.id).where(SessionRow.active_space_id == space_id))
        return next((row["id"] for row in self.repository.materials.assessment_data["sessions"].values()
                     if row["space_id"] == space_id and row["status"] == "active"), None)

    def _safe_context(self, space, task):
        topic_ids = task["topic_ids"]
        topics = [t for t in self.spaces.bound_topics(space) if t["id"] in topic_ids]
        return {"task_id": task["id"], "topic_ids": copy.deepcopy(topic_ids), "kind": task["kind"],
                "estimated_minutes": task["estimated_minutes"], "reason": task["reason"],
                "topics": [{"id": t["id"], "name": t["name"]} for t in topics],
                "source_refs": [{k: ref.get(k) for k in ("material_id", "material_version_id", "chunk_id", "page", "line_start", "line_end")}
                                for t in topics for ref in t.get("source_refs", [])]}

    def _planning_states(self, space_id):
        states = {row["topic_id"]: row for row in self.repository.records("states", space_id=space_id, lock=False)}
        evidence = self.repository.records("evidence", space_id=space_id, lock=False)
        for topic_id, state in states.items():
            valid = [e for e in evidence if e["topic_id"] == topic_id and e.get("eligible") and not e.get("assisted")
                     and e.get("topic_revision_id") == state.get("topic_revision_id") and e["id"] in state.get("evidence_ids", [])]
            if valid:
                submissions = len({e["assessment_id"] for e in valid})
                days = self.policy.review_intervals[min(submissions - 1, len(self.policy.review_intervals) - 1)]
                latest = max(datetime.fromisoformat(e["created_at"]) for e in valid)
                state["review_due_at"] = (latest + timedelta(days=days)).isoformat()
        return states

    def _snapshot(self, space, states):
        return {"scope_version": space["scope_version"], "profile_version": space["profile_version"],
                "bindings": copy.deepcopy(space["bindings"]), "states": copy.deepcopy(states),
                "policy_version": self.policy.version}

    def _ordered_topics(self, space, state_rows):
        topics = self.spaces.bound_topics(space)
        selected = set(space.get("topic_ids") or [])
        excluded = set(space.get("excluded_topic_ids") or [])
        topics = [copy.deepcopy(t) for t in topics if t.get("status", "active") == "active" and (not selected or t["id"] in selected) and t["id"] not in excluded]
        available = {t["id"] for t in topics}
        by_id = {t["id"]: t for t in topics}
        for topic in topics:
            for prerequisite in topic.get("prerequisites") or []:
                if prerequisite not in available:
                    conflict("所选范围缺少必要前置知识", ["missing_prerequisite", prerequisite], ["将前置知识加入范围", "移除依赖该前置的主题"])
        visiting, visited, ordered = set(), set(), []
        def visit(topic_id):
            if topic_id in visiting:
                conflict("知识点前置关系存在环", ["prerequisite_cycle"], ["审核并修正前置关系"])
            if topic_id in visited:
                return
            visiting.add(topic_id)
            for prerequisite in by_id[topic_id].get("prerequisites") or []:
                visit(prerequisite)
            visiting.remove(topic_id)
            visited.add(topic_id)
            ordered.append(by_id[topic_id])
        for topic in topics:
            visit(topic["id"])
        rank = {"needs_review": 0, "unstable": 1, "learning": 2, "mastered": 3, "unseen": 4}
        # Kahn traversal: only currently-ready nodes are priority sorted, so
        # a weak dependent can never leap over an unmet prerequisite.
        remaining = {t["id"]: t for t in ordered}
        result = []
        while remaining:
            ready = [t for t in remaining.values() if all(p not in remaining for p in (t.get("prerequisites") or []))]
            if not ready:
                raise DomainConflict("PLAN_CONSTRAINT_UNSATISFIABLE", "知识点前置关系存在环")
            ready.sort(key=lambda t: (rank.get(state_rows.get(t["id"], {}).get("status", "unseen"), 4),
                                      -(len(state_rows.get(t["id"], {}).get("error_tags") or [])),
                                      state_rows.get(t["id"], {}).get("last_assessed_at") or "9999", t["id"]))
            topic = ready[0]
            result.append(topic)
            remaining.pop(topic["id"], None)
        return result

    def _validate_request(self, space, payload):
        payload = dict(payload or {})
        count = payload.get("session_count", 3)
        minutes = payload.get("minutes_per_session", 30)
        include_review = payload.get("include_review", True)
        mode = payload.get("rebuild_mode", "initial")
        if type(count) is not int or not 3 <= count <= 5:
            raise DomainConflict("INVALID_SESSION_COUNT", "session_count 必须在 3 到 5 之间")
        if type(minutes) is not int or not 10 <= minutes <= 120:
            raise DomainConflict("INVALID_SESSION_DURATION", "minutes_per_session 必须在 10 到 120 之间")
        if type(include_review) is not bool:
            raise DomainConflict("INVALID_REQUEST", "include_review 必须是布尔值")
        if mode not in {"initial", "local_replan"}:
            raise DomainConflict("INVALID_REBUILD_MODE", "rebuild_mode 不合法")
        target = space.get("target_date")
        if target:
            try:
                if date.fromisoformat(target) < datetime.fromisoformat(now()).date():
                    conflict("目标日期已过期", ["target_date"], ["更新 target_date"])
            except ValueError:
                conflict("目标日期无效", ["target_date"], ["更新 target_date"])
        if mode == "local_replan" and (not payload.get("base_plan_id") or type(payload.get("expected_plan_version")) is not int):
            raise DomainConflict("INVALID_REQUEST", "local_replan 必须提供 base_plan_id 和 expected_plan_version")
        return {"session_count": count, "minutes_per_session": minutes, "include_review": include_review,
                "rebuild_mode": mode, "base_plan_id": payload.get("base_plan_id"),
                "expected_plan_version": payload.get("expected_plan_version")}

    def _task_payload(self, row):
        if isinstance(row, dict):
            return copy.deepcopy(row)
        return {"id": row.id, "topic_ids": copy.deepcopy(row.topic_ids), "kind": row.kind,
                "status": row.status, "estimated_minutes": row.estimated_minutes, "reason": row.reason,
                "note": row.note, "defer_until": iso(row.defer_until), "context": copy.deepcopy(row.context or {})}

    def _plan_payload(self, plan, tasks):
        if isinstance(plan, dict):
            result = copy.deepcopy(plan)
        else:
            result = {"plan_id": plan.id, "id": plan.id, "space_id": plan.space_id, "version": plan.version,
                      "status": plan.status, "scope_version": plan.scope_version, "run_id": plan.run_id,
                      "config": copy.deepcopy(plan.config or {}), "created_at": iso(plan.created_at)}
        result["plan_id"] = result.get("plan_id") or result["id"]
        result["id"] = result["plan_id"]
        result["tasks"] = [self._task_payload(t) for t in tasks]
        return result

    def _get_plan_sql(self, plan_id, lock=False):
        with self.uow.session() as session:
            query = select(StudyPlanRow).where(StudyPlanRow.id == plan_id)
            if lock and self.uow.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            plan = session.scalar(query)
            if plan is None:
                raise DomainNotFound("plan", plan_id)
            task_query = select(StudyTaskRow).where(StudyTaskRow.plan_id == plan_id).order_by(StudyTaskRow.id)
            if lock and self.uow.active:
                task_query = task_query.with_for_update()
            tasks = list(session.scalars(task_query))
            tasks.sort(key=lambda task: ((task.context or {}).get("position", 10**9), task.id))
            return plan, tasks

    def create_plan(self, space_id, payload, idempotency_key=None):
        payload = dict(payload or {})
        def change():
            space = self.spaces.repository.get(space_id)
            config = self._validate_request(space, payload)
            base = None
            if config["rebuild_mode"] == "local_replan":
                base = self._get_plan_sql(config["base_plan_id"], lock=True) if self.sql else self._load_plan_memory(config["base_plan_id"])
                if base is None:
                    raise DomainNotFound("plan", config["base_plan_id"])
                base_space = base[0].space_id if self.sql else base["space_id"]
                if base_space != space_id:
                    raise DomainConflict("PLAN_SPACE_MISMATCH", "基础计划不属于当前学习空间")
                base_status = base[0].status if self.sql else base["status"]
                if base_status == "superseded":
                    raise DomainConflict("PLAN_NOT_ACTIVE", "基础计划已被替代")
                base_version = base[0].version if self.sql else base["version"]
                if base_version != config["expected_plan_version"]:
                    raise DomainConflict("VERSION_CONFLICT", "计划版本已变化", {"latest_plan": self._plan_payload(*base) if self.sql else copy.deepcopy(base)})
            states = self._planning_states(space_id)
            topics = self._ordered_topics(space, states)
            if not topics:
                raise DomainConflict("PLAN_CONSTRAINT_UNSATISFIABLE", "当前学习范围没有可安排的知识点", {"conflicts": ["empty_scope"], "adjustable_constraints": ["选择至少一个知识点"]})
            old_tasks = ([self._task_payload(t) for t in base[1]] if self.sql else base.get("tasks", [])) if base else []
            tasks = build_tasks(topics, states, config, space, old_tasks, now(), self.policy)
            config["snapshot"] = self._snapshot(space, states)
            plan_id = uid()
            run = self.runs.create("plan_build", {"type": "plan", "id": plan_id}, status="succeeded")
            timestamp = parse_dt(now())
            if self.sql:
                session = self.uow._current.get()
                row = StudyPlanRow(id=plan_id, space_id=space_id, version=1, status="ready", scope_version=space["scope_version"],
                                   run_id=run["id"], config=config, created_at=timestamp)
                session.add(row)
                session.flush()
                session.add_all(StudyTaskRow(id=t["id"], plan_id=plan_id, topic_ids=t["topic_ids"], kind=t["kind"], status=t["status"], context=t.get("context", {}),
                                             estimated_minutes=t["estimated_minutes"], reason=t["reason"], note=t["note"],
                                             defer_until=parse_dt(t["defer_until"])) for t in tasks)
                if base:
                    base_row = base[0]
                    base_row.status = "superseded"
                    base_row.version += 1
            else:
                plan = {"plan_id": plan_id, "id": plan_id, "space_id": space_id, "version": 1, "status": "ready",
                        "scope_version": space["scope_version"], "run_id": run["id"], "config": config,
                        "created_at": now(), "tasks": tasks}
                self.memory_plans[plan_id] = plan
                self.repository.materials.assessment_data["plans"][plan_id] = copy.deepcopy(plan)
                if base:
                    base["status"] = "superseded"
                    base["version"] += 1
                    self.repository.materials.assessment_data["plans"][base["id"]] = copy.deepcopy(base)
            result = {"plan_id": plan_id, "id": plan_id, "space_id": space_id, "version": 1, "status": "ready",
                      "scope_version": space["scope_version"], "run_id": run["id"], "config": config, "tasks": tasks, "created_at": iso(timestamp)}

            return copy.deepcopy(result)
        return self.assessments._execute("plan.create", space_id, payload, idempotency_key, change)

    def get_plan(self, plan_id):
        if self.sql:
            plan, tasks = self._get_plan_sql(plan_id)
            result = self._plan_payload(plan, tasks)
        else:
            plan = self._load_plan_memory(plan_id)
            if plan is None:
                raise DomainNotFound("plan", plan_id)
            result = copy.deepcopy(plan)
        space = self.spaces.repository.get(result["space_id"])
        current = self._snapshot(space, self._planning_states(space["id"]))
        saved = result.get("config", {}).get("snapshot")
        if result["status"] == "ready" and saved != current:
            result["status"] = "needs_replan"
        return result

    def update_task(self, plan_id, task_id, payload):
        payload = dict(payload or {})
        expected = payload.get("expected_plan_version")
        status = payload.get("status")
        if type(expected) is not int:
            raise DomainConflict("INVALID_REQUEST", "expected_plan_version 必填")
        if status not in {"completed", "skipped", "deferred"}:
            raise DomainConflict("INVALID_TASK_STATUS", "任务状态不合法")
        if status == "skipped" and not str(payload.get("reason") or "").strip():
            raise DomainConflict("SKIP_REASON_REQUIRED", "跳过任务必须提供 reason")
        if status == "deferred":
            value = payload.get("defer_until")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
                if parsed is None or parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise DomainConflict("DEFER_UNTIL_REQUIRED", "延后任务必须提供带时区的 UTC defer_until") from None
            if parsed.utcoffset().total_seconds() != 0:
                raise DomainConflict("DEFER_UNTIL_REQUIRED", "defer_until 必须是 UTC 时间")
        with self._tx():
            if self.sql:
                plan_row, _ = self._get_plan_sql(plan_id, lock=False)
                self.spaces.repository.get(plan_row.space_id)
                plan_row, tasks = self._get_plan_sql(plan_id, lock=True)
                if plan_row.version != expected:
                    raise DomainConflict("VERSION_CONFLICT", "计划版本已变化", {"latest_plan": self._plan_payload(plan_row, tasks)})
                task = next((t for t in tasks if t.id == task_id), None)
                if task is None:
                    raise DomainNotFound("task", task_id)
                task.status = status
                task.note = payload.get("note") or payload.get("reason")
                task.defer_until = parse_dt(payload.get("defer_until")) if status == "deferred" else None
                plan_row.version += 1
                return self._task_payload(task) | {"plan_id": plan_id, "plan_version": plan_row.version}
            plan = self._load_plan_memory(plan_id)
            if plan is None:
                raise DomainNotFound("plan", plan_id)
            if plan["version"] != expected:
                raise DomainConflict("VERSION_CONFLICT", "计划版本已变化", {"latest_plan": copy.deepcopy(plan)})
            task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if task is None:
                raise DomainNotFound("task", task_id)
            task.update(status=status, note=payload.get("note") or payload.get("reason"), defer_until=payload.get("defer_until") if status == "deferred" else None)
            plan["version"] += 1
            self.repository.materials.assessment_data["plans"][plan_id] = copy.deepcopy(plan)
            return copy.deepcopy(task) | {"plan_id": plan_id, "plan_version": plan["version"]}

    def start_session(self, plan_id, task_id, idempotency_key=None):
        payload = {"task_id": task_id}
        def change():
            if self.get_plan(plan_id)["status"] != "ready":
                raise DomainConflict("PLAN_NOT_ACTIVE", "计划已被替代或需要重新规划")
            if self.sql:
                plan, _ = self._get_plan_sql(plan_id, lock=False)
                space = self.spaces.repository.get(plan.space_id)
                plan, tasks = self._get_plan_sql(plan_id, lock=True)
                if self.get_plan(plan_id)["status"] != "ready":
                    raise DomainConflict("PLAN_NOT_ACTIVE", "计划已被替代或需要重新规划")
                task = next((t for t in tasks if t.id == task_id), None)
                if task is None:
                    raise DomainNotFound("task", task_id)
                if task.status in {"completed", "skipped"}:
                    raise DomainConflict("TASK_NOT_ACTIVE", "已完成或跳过的任务不能开始会话")
                session = self.uow._current.get()
                active = session.scalar(select(SessionRow).where(SessionRow.space_id == space["id"], SessionRow.status == "active").with_for_update())
                if active is not None:
                    raise DomainConflict("SESSION_ACTIVE", "学习空间已有活动会话")
                row = SessionRow(id=uid(), space_id=space["id"], plan_id=plan_id, task_id=task_id, status="active", active_space_id=space["id"], context=self._safe_context(space, self._task_payload(task)), started_at=parse_dt(now()), finished_at=None)
                session.add(row)
                result = {"id": row.id, "session_id": row.id, "plan_id": plan_id, "task_id": task_id, "status": "active", "started_at": iso(row.started_at), "context": copy.deepcopy(row.context)}

                return result
            plan = self._load_plan_memory(plan_id)
            if plan is None:
                raise DomainNotFound("plan", plan_id)
            space = self.spaces.repository.get(plan["space_id"])
            task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if task is None:
                raise DomainNotFound("task", task_id)
            if task["status"] in {"completed", "skipped"}:
                raise DomainConflict("TASK_NOT_ACTIVE", "已完成或跳过的任务不能开始会话")
            if any(s["space_id"] == space["id"] and s["status"] == "active" for s in self.repository.materials.assessment_data["sessions"].values()):
                raise DomainConflict("SESSION_ACTIVE", "学习空间已有活动会话")
            session = {"id": uid(), "session_id": None, "plan_id": plan_id, "space_id": space["id"], "task_id": task_id, "status": "active", "started_at": now(), "finished_at": None, "events": [], "context": self._safe_context(space, task)}
            session["session_id"] = session["id"]
            self.memory_sessions[session["id"]] = session
            self.repository.materials.assessment_data["sessions"][session["id"]] = copy.deepcopy(session)
            result = copy.deepcopy({k: v for k, v in session.items() if k != "events"})

            return result
        return self.assessments._execute("session.start", plan_id, payload, idempotency_key, change)

    def _find_session_sql(self, session_id, lock=False):
        session = self.uow._current.get()
        query = select(SessionRow).where(SessionRow.id == session_id)
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise DomainNotFound("session", session_id)
        return row

    def add_event(self, session_id, payload, idempotency_key=None):
        payload = copy.deepcopy(payload or {})
        event_type = payload.get("type")
        if event_type not in self.EVENT_TYPES:
            raise DomainConflict("INVALID_EVENT_TYPE", "会话事件类型不合法")
        event_id = payload.get("event_id")
        fingerprint = self._fingerprint("session.event", session_id, payload)
        # Stable client event IDs also act as durable replay keys.
        key = idempotency_key or ("event:" + sha256(event_id.encode()).hexdigest() if event_id else None)

        def change():
            if self.sql:
                row = self._find_session_sql(session_id)
                space_id = row.space_id
                db = self.uow._current.get()
                existing = db.scalar(select(SessionEventRow).where(SessionEventRow.client_event_id == event_id)) if event_id else None
                if existing is not None:
                    if existing.session_id != session_id or existing.payload.get("fingerprint") != fingerprint:
                        raise DomainConflict("IDEMPOTENCY_CONFLICT", "event_id 已用于不同请求")
                    return copy.deepcopy(existing.payload["response"])
            else:
                row = self._load_session_memory(session_id)
                if row is None:
                    raise DomainNotFound("session", session_id)
                space_id = row["space_id"]
                existing = next((e for e in self.repository.materials.assessment_data["session_events"].values()
                                 if event_id and e.get("event_id") == event_id), None)
                if existing is not None:
                    if existing["session_id"] != session_id or existing["fingerprint"] != fingerprint:
                        raise DomainConflict("IDEMPOTENCY_CONFLICT", "event_id 已用于不同请求")
                    return copy.deepcopy(existing["response"])
            # Assessment -> space -> session is the shared lock order with grading.
            # All writes, including assisted, roll back if the session rejects the event.
            if event_type == "request_hint" and payload.get("question_id"):
                self.assessments.mark_assisted(space_id, payload["question_id"])
            space = self.spaces.repository.get(space_id)
            if self.sql:
                row = self._find_session_sql(session_id, lock=True)
                status = row.status
            else:
                row = self._load_session_memory(session_id)
                status = row["status"]
            if status != "active":
                raise DomainConflict("SESSION_FINISHED", "会话已结束")
            topic_id = payload.get("topic_id")
            if topic_id and topic_id not in {t["id"] for t in self.assessments._topics(space)}:
                raise DomainConflict("TOPIC_OUT_OF_SCOPE", "事件知识点不属于当前学习范围")
            response = {"id": uid(), "event_id": event_id, "type": event_type,
                        "topic_id": topic_id, "question_id": payload.get("question_id"), "received_at": now()}
            stored = {**payload, "fingerprint": fingerprint, "response": response}
            if self.sql:
                db.add(SessionEventRow(id=response["id"], session_id=session_id, client_event_id=event_id,
                                       type=event_type, payload=stored, received_at=parse_dt(response["received_at"])))
            else:
                self.repository.materials.assessment_data["session_events"][response["id"]] = {
                    **stored, "session_id": session_id}
                row["events"].append(copy.deepcopy(response))
            return copy.deepcopy(response)
        return self.assessments._execute("session.event", session_id, payload, key, change)

    def has_history(self, space_id):
        if self.sql:
            with self.uow.session() as session:
                if session.scalar(select(StudyPlanRow.id).where(StudyPlanRow.space_id == space_id).limit(1)):
                    return True
                return bool(session.scalar(select(SessionRow.id).where(SessionRow.space_id == space_id).limit(1)))
        return any(p.get("space_id") == space_id for p in self.repository.materials.assessment_data["plans"].values()) or any(s.get("space_id") == space_id for s in self.repository.materials.assessment_data["sessions"].values())

    def finish_session(self, session_id):
        with self._tx():
            if self.sql:
                row = self._find_session_sql(session_id, lock=False)
                self.spaces.repository.get(row.space_id)
                row = self._find_session_sql(session_id, lock=True)
                if row.status == "finished":
                    elapsed = int(max(0, (row.finished_at - row.started_at).total_seconds())) if row.finished_at else None
                    return {"session_id": row.id, "status": "finished", "elapsed_seconds": elapsed, "reported_active_seconds": None, "task_ids": [row.task_id]}
                row.status = "finished"
                row.active_space_id = None
                row.finished_at = parse_dt(now())
                elapsed = int(max(0, (row.finished_at - row.started_at).total_seconds()))
                return {"session_id": row.id, "status": "finished", "elapsed_seconds": elapsed, "reported_active_seconds": None, "task_ids": [row.task_id]}
            session = self._load_session_memory(session_id)
            if session is None:
                raise DomainNotFound("session", session_id)
            if session["status"] == "finished":
                elapsed = session.get("elapsed_seconds")
            else:
                session["status"] = "finished"
                session["finished_at"] = now()
                elapsed = int(max(0, (datetime.fromisoformat(session["finished_at"]) - datetime.fromisoformat(session["started_at"])).total_seconds()))
                session["elapsed_seconds"] = elapsed
                self.repository.materials.assessment_data["sessions"][session_id] = copy.deepcopy(session)
            return {"session_id": session["id"], "status": "finished", "elapsed_seconds": elapsed, "reported_active_seconds": None, "task_ids": [session["task_id"]]}
