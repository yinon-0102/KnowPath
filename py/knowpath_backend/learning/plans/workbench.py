"""Read projections for resumable workbenches and canonical learning history."""
from __future__ import annotations

import base64
import binascii
import copy
import json
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.persistence.db import StudyPlanRow, StudyTaskRow, SessionRow, SessionEventRow


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def sources(refs):
    """Expose source locators without accidentally returning frozen source text."""
    allowed = {"material_id", "material_version_id", "chunk_id", "page", "line_start", "line_end",
               "section", "section_path", "source_unit_id", "source_unit_ids", "document_id"}
    return [{key: copy.deepcopy(value) for key, value in ref.items() if key in allowed}
            for ref in refs or [] if isinstance(ref, dict)]


class WorkbenchReads:
    def _space_plans(self, space_id):
        if not self.sql:
            result = [self._plan_payload(plan, plan.get("tasks", []))
                      for plan in self.repository.records("plans", space_id=space_id, lock=False)]
            self._resolve_legacy_nodes(result)
            return result
        with self.uow.session() as session:
            plan_query = select(StudyPlanRow).where(StudyPlanRow.space_id == space_id)
            task_query = select(StudyTaskRow).join(StudyPlanRow).where(StudyPlanRow.space_id == space_id)
            if self.uow.active:
                # Space commands already hold the space lock. MySQL's ordinary
                # snapshot can predate that lock; current reads must also refresh
                # objects previously loaded into this transaction's identity map.
                plan_query = plan_query.with_for_update().execution_options(populate_existing=True)
                task_query = task_query.with_for_update().execution_options(populate_existing=True)
            plans = list(session.scalars(plan_query))
            tasks = list(session.scalars(task_query))
            by_plan = {}
            for task in tasks:
                by_plan.setdefault(task.plan_id, []).append(task)
            result = []
            for plan in plans:
                ordered = sorted(by_plan.get(plan.id, []), key=lambda task: (
                    (task.context or {}).get("position", 10**9), task.id))
                result.append(self._plan_payload(plan, ordered))
            self._resolve_legacy_nodes(result)
            return result

    def _current_plan_id(self, space_id):
        plan = max((row for row in self._space_plans(space_id) if row["status"] != "superseded"),
                   key=self._plan_key, default=None)
        return plan["id"] if plan else None

    @staticmethod
    def _plan_key(plan):
        return timestamp(plan["created_at"]), plan["id"]

    @staticmethod
    def _resolve_legacy_nodes(plans):
        """Resolve only proven legacy copies; origin alone may mark a new cycle."""
        tasks = {task['id']: task for plan in plans for task in plan['tasks']}
        resolved = set()
        for task in tasks.values():
            # Resolve ancestors first, including plans stored in the same second.
            # Use an iterative walk so a long legacy history cannot overflow.
            chain, visiting = [], set()
            current = task
            while current is not None and current['id'] not in resolved:
                if current['id'] in visiting:
                    resolved.update(visiting)  # A cycle is not proof of inheritance.
                    break
                visiting.add(current['id'])
                chain.append(current)
                current = tasks.get(current.get('context', {}).get('origin_task_id'))
            for item in reversed(chain):
                if item['id'] in resolved:
                    continue
                context = item.get('context', {})
                previous = tasks.get(context.get('origin_task_id'))
                if 'node_id' not in context and previous is not None and previous['id'] in resolved:
                    old = previous.get('context', {})
                    same_cycle = bool(context.get('cycle_fingerprint') and
                                      context['cycle_fingerprint'] == old.get('cycle_fingerprint'))
                    same_input = bool(context.get('input_fingerprint') and
                                      context['input_fingerprint'] == old.get('input_fingerprint')
                                      and item['kind'] == previous['kind'])
                    if context.get('historical') or same_cycle or same_input:
                        item['node_id'] = previous['node_id']
                        item['sequence'] = previous['sequence']
                resolved.add(item['id'])

    def _space_sessions(self, space_id):
        from knowpath_backend.learning.plans.service import iso, now
        if self.sql:
            with self.uow.session() as session:
                raw = list(session.scalars(select(SessionRow).where(SessionRow.space_id == space_id)))
                rows = [{"id": row.id, "session_id": row.id, "space_id": row.space_id,
                         "plan_id": row.plan_id, "task_id": row.task_id, "status": row.status,
                         "started_at": iso(row.started_at), "finished_at": iso(row.finished_at),
                         "context": copy.deepcopy(row.context or {})} for row in raw]
                events = list(session.scalars(select(SessionEventRow).join(SessionRow)
                                               .where(SessionRow.space_id == space_id)))
                by_session = {}
                for event in events:
                    by_session.setdefault(event.session_id, []).append(
                        {"id": event.id, "type": event.type, "received_at": iso(event.received_at)})
        else:
            rows = self.repository.records("sessions", space_id=space_id, lock=False)
            by_session = {}
            # Legacy memory events store their id inside response, rather than on the wrapper.
            for event in self.repository.materials.assessment_data["session_events"].values():
                response = event.get("response", {})
                by_session.setdefault(event.get("session_id"), []).append(response)
        for row in rows:
            context = row.get("context") or {}
            row["session_id"] = row["id"]
            row["paused"] = bool(context.get("paused", False))
            if "paused" not in context:
                for event in sorted(by_session.get(row["id"], []), key=lambda e: (
                        e.get("received_at", ""), e.get("id", ""))):
                    if event.get("type") in {"pause", "resume"}:
                        row["paused"] = event["type"] == "pause"
            if row["status"] != "active":
                row["paused"] = False
            elapsed = context.get("finish_elapsed_seconds", row.get("elapsed_seconds"))
            if elapsed is None and row.get("started_at") and (row.get("finished_at") or row["status"] == "active"):
                finish = row.get("finished_at") or now()
                try:
                    elapsed = int(max(0, (datetime.fromisoformat(timestamp(finish))
                        - datetime.fromisoformat(timestamp(row["started_at"]))).total_seconds()))
                except (ValueError, TypeError):
                    elapsed = None
            row["elapsed_seconds"] = max(0, int(elapsed)) if elapsed is not None else None
        return sorted(rows, key=lambda row: (timestamp(row["started_at"]) if row.get("started_at") else "", row["id"]))

    def get_workbench(self, space_id):
        self.spaces.repository.get(space_id)
        plans = sorted(self._space_plans(space_id), key=self._plan_key, reverse=True)
        current = next((plan for plan in plans if plan["status"] != "superseded"), None)
        if current is not None:
            resolved = {task["id"]: task for task in current["tasks"]}
            current = self.get_plan(current["id"])
            for task in current["tasks"]:
                task.update(node_id=resolved[task["id"]]["node_id"], sequence=resolved[task["id"]]["sequence"])
        sessions = self._space_sessions(space_id)
        active = next((row for row in reversed(sessions) if row["status"] == "active"), None)
        if active is not None:
            active = {key: copy.deepcopy(active[key]) for key in (
                "id", "session_id", "space_id", "plan_id", "task_id", "status", "started_at", "paused", "context")}
            active["context"] = self._record_context(active["context"])
        return {"space_id": space_id, "current_plan": current, "active_session": active}

    def _canonical_nodes(self, plans, sessions):
        """A node occurs in its first round and points to its latest plan copy."""
        groups = {}
        for plan in sorted(plans, key=self._plan_key):
            for task in plan["tasks"]:
                groups.setdefault(task["node_id"], []).append((plan, task))
        active = {row["task_id"]: row["id"] for row in sessions if row["status"] == "active"}
        rounds = {plan["id"]: [] for plan in plans}
        for node_id, copies in groups.items():
            by_task = {task['id']: task for _, task in copies}
            def copy_key(copy):
                plan, task = copy
                ancestor = task
                seen = {task['id']}
                depth = 0
                while True:
                    parent = by_task.get(ancestor.get('context', {}).get('origin_task_id'))
                    if parent is None or parent['id'] in seen:
                        break
                    depth += 1
                    seen.add(parent['id'])
                    ancestor = parent
                return depth, self._plan_key(plan)
            # UUID order is only a pagination tie-breaker, never inheritance order.
            earliest_plan, earliest_task = min(copies, key=copy_key)
            latest_plan, _ = max(copies, key=copy_key)
            latest_copies = [task for plan, task in copies if plan["id"] == latest_plan["id"]]
            task = min(latest_copies, key=lambda t: (bool(t.get("context", {}).get("historical")),
                                                      t.get("sequence", 0), t["id"]))
            ids = {copy_task["id"] for _, copy_task in copies}
            active_id = next((sid for tid, sid in active.items() if tid in ids), None)
            context = task.get("context", {})
            title = context.get("title") or earliest_task.get("context", {}).get("title")
            if not title:
                frozen = context.get("topics", [])
                title = "、".join(topic.get("name", "") for topic in frozen) or task["reason"]
            historical = bool(context.get("historical"))
            status = "active" if active_id and task["status"] not in {"completed", "skipped", "deferred"} else task["status"]
            rounds[earliest_plan["id"]].append({"node_id": node_id, "plan_id": latest_plan["id"],
                "task_id": task["id"], "sequence": earliest_task["sequence"], "title": title,
                "kind": task["kind"], "status": status, "topic_ids": copy.deepcopy(task["topic_ids"]),
                "estimated_minutes": task["estimated_minutes"], "active_session_id": active_id,
                "historical": historical})
        for nodes in rounds.values():
            nodes.sort(key=lambda node: (node["sequence"], node["node_id"]))
        return rounds

    def get_progress(self, space_id, *, limit=10, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainConflict("INVALID_LIMIT", "limit 必须为 1—50")
        self.spaces.repository.get(space_id)
        fingerprint = sha256(json.dumps(["workbench.progress", space_id]).encode()).hexdigest()
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                    raise ValueError
                token = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
                if (not isinstance(token, list) or len(token) != 4 or token[0] != 1 or token[1] != fingerprint
                        or not isinstance(token[2], str) or not isinstance(token[3], str) or not token[3]):
                    raise ValueError
                after = (timestamp(token[2]), token[3])
            except (ValueError, TypeError, UnicodeError, binascii.Error):
                raise DomainConflict("INVALID_CURSOR", "分页游标无效或不属于当前查询") from None
        plans = sorted(self._space_plans(space_id), key=self._plan_key, reverse=True)
        nodes = self._canonical_nodes(plans, self._space_sessions(space_id))
        all_nodes = [node for rows in nodes.values() for node in rows]
        remaining = [plan for plan in plans if after is None or self._plan_key(plan) < after]
        page = remaining[:limit]
        next_cursor = None
        if len(remaining) > limit:
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                [1, fingerprint, *self._plan_key(page[-1])], separators=(",", ":")).encode()).decode()
        rounds = [{"plan_id": plan["id"], "created_at": plan["created_at"], "status": plan["status"],
                   "nodes": nodes[plan["id"]]} for plan in page]
        if rounds and after is None and page[0]["status"] != "superseded":
            rounds[0]["status"] = self.get_plan(page[0]["id"])["status"]
        return {"space_id": space_id, "rounds": rounds,
                "completed_count": sum(node["status"] == "completed" for node in all_nodes),
                "total_count": len(all_nodes), "next_cursor": next_cursor}

    @staticmethod
    def _record_context(context):
        allowed = {"task_id", "node_id", "topic_ids", "kind", "estimated_minutes", "reason",
                   "topics", "source_refs", "paused", "finish_elapsed_seconds"}
        result = {key: copy.deepcopy(value) for key, value in context.items() if key in allowed}
        if "source_refs" in result:
            result["source_refs"] = sources(result["source_refs"])
        if "topics" in result:
            result["topics"] = [{"id": topic["id"], "name": topic.get("name", topic["id"]),
                                 **({"source_refs": sources(topic["source_refs"])} if "source_refs" in topic else {})}
                                for topic in result["topics"]]
        return result

    def _record_topics(self, space, task, sessions):
        current = {topic["id"]: topic for topic in self.spaces.bound_topics(space)}
        result = []
        for topic_id in task["topic_ids"]:
            saved = None
            origin = None
            for session in sessions:
                context = session.get("context", {})
                saved = next((topic for topic in context.get("topics", []) if topic.get("id") == topic_id), None)
                if saved is not None:
                    saved = copy.deepcopy(saved)
                    # Older one-topic sessions stored locators at context level.
                    if "source_refs" not in saved and len(context.get("topic_ids", [])) == 1 and "source_refs" in context:
                        saved["source_refs"] = copy.deepcopy(context["source_refs"])
                    origin = "session_snapshot"
                    break
            if saved is None:
                saved = next((topic for topic in task.get("context", {}).get("topics", [])
                              if topic.get("id") == topic_id), None)
                if saved is not None:
                    saved, origin = copy.deepcopy(saved), "task_snapshot"
            fallback = False
            if saved is None:
                saved = copy.deepcopy(current.get(topic_id, {"id": topic_id, "name": topic_id, "source_refs": []}))
                origin = "current" if topic_id in current else "missing"
                fallback = True
            elif "source_refs" not in saved:
                saved["source_refs"] = copy.deepcopy(current.get(topic_id, {}).get("source_refs", []))
                fallback = True
            result.append({"id": topic_id, "name": saved.get("name", topic_id),
                           "source_refs": sources(saved.get("source_refs", [])), "source": origin,
                           "fallback": fallback})
        return result

    def get_learning_record(self, plan_id, task_id):
        plan = self.get_plan(plan_id)
        plans = self._space_plans(plan["space_id"])
        resolved_plan = next(row for row in plans if row["id"] == plan_id)
        task = next((row for row in resolved_plan["tasks"] if row["id"] == task_id), None)
        if task is None:
            raise DomainNotFound("task", task_id)
        space = self.spaces.repository.get(plan["space_id"])
        copies = {(copy_plan["id"], row["id"]) for copy_plan in plans for row in copy_plan["tasks"]
                  if row["node_id"] == task["node_id"]}
        sessions = [row for row in self._space_sessions(plan["space_id"])
                    if (row["plan_id"], row["task_id"]) in copies]
        session_ids = {row["id"] for row in sessions}
        assessments = []
        for row in self.repository.records("assessments", space_id=plan["space_id"], lock=False):
            # Explicit IDs are the sole association evidence for legacy records.
            if ((row.get("plan_id"), row.get("task_id")) not in copies
                    and row.get("learning_session_id") not in session_ids):
                continue
            assessments.append({"assessment_id": row["id"], "kind": row["kind"], "status": row["status"],
                "created_at": row["created_at"], "learning_session_id": row.get("learning_session_id"),
                "result": self.assessments.result(row["id"]) if row["status"] == "completed" else None})
        current_plan = max((row for row in plans if row["status"] != "superseded"),
                           key=self._plan_key, default=None)
        current = bool(current_plan and current_plan["id"] == plan_id and not task.get("context", {}).get("historical"))
        return {"space_id": plan["space_id"], "plan_id": plan_id, "task_id": task_id,
                "node_id": task["node_id"], "task": task, "current": current, "plan_status": plan["status"],
                "topics": self._record_topics(space, task, sessions),
                "sessions": [{**{key: copy.deepcopy(row.get(key)) for key in (
                    "session_id", "plan_id", "task_id", "status", "started_at", "finished_at", "elapsed_seconds", "paused")},
                    "context": self._record_context(row.get("context", {}))} for row in sessions],
                "assessments": assessments, "total_elapsed_seconds": (sum(row["elapsed_seconds"] for row in sessions)
                    if all(row["elapsed_seconds"] is not None for row in sessions) else None)}

    def validate_assessment_links(self, space, payload):
        plan_id, task_id, session_id = (payload.get(key) for key in ("plan_id", "task_id", "learning_session_id"))
        if not any((plan_id, task_id, session_id)):
            return {}
        if session_id:
            linked_session = self.repository.get_record("sessions", session_id, lock=False)
            if linked_session["space_id"] != space["id"]:
                raise DomainConflict("SESSION_SPACE_MISMATCH", "学习会话不属于当前学习空间")
            if ((plan_id and plan_id != linked_session["plan_id"])
                    or (task_id and task_id != linked_session["task_id"])):
                raise DomainConflict("SESSION_TASK_MISMATCH", "学习会话不属于指定计划任务")
            plan_id, task_id = linked_session["plan_id"], linked_session["task_id"]
        if task_id and not plan_id:
            raise DomainConflict("PLAN_ID_REQUIRED", "关联任务必须提供 plan_id")
        ownership = self.repository.get_record("plans", plan_id, lock=False)
        if ownership["space_id"] != space["id"]:
            raise DomainConflict("PLAN_SPACE_MISMATCH", "关联计划不属于当前学习空间")
        plan = self.get_plan(plan_id, lock=True)
        if plan["status"] == "superseded":
            raise DomainConflict("PLAN_NOT_ACTIVE", "关联计划已被替代")
        links = {"plan_id": plan_id}
        if task_id:
            task = next((row for row in plan["tasks"] if row["id"] == task_id), None)
            if task is None:
                raise DomainNotFound("task", task_id)
            if (task.get("context", {}).get("historical") or task["status"] in {"skipped", "deferred"}
                    or not set(task["topic_ids"]).issubset({topic["id"] for topic in self.assessments._topics(space)})):
                raise DomainConflict("TASK_NOT_ACTIVE", "历史、范围外、跳过或延期的任务不能创建测评")
            if task["status"] == "completed" and payload["kind"] != "retest":
                raise DomainConflict("TASK_NOT_ACTIVE", "已完成任务仅支持学习后复测")
            if plan["status"] != "ready" and not (task["status"] == "completed" and payload["kind"] == "retest"):
                raise DomainConflict("PLAN_NOT_ACTIVE", "关联计划需要重新规划")
            requested = payload.get("topic_ids") or task["topic_ids"]
            if not set(requested).issubset(task["topic_ids"]):
                raise DomainConflict("TOPIC_TASK_MISMATCH", "测评主题不属于指定任务")
            payload["topic_ids"] = copy.deepcopy(requested)
            links["task_id"] = task_id
        if session_id:
            links["learning_session_id"] = session_id
        return links
