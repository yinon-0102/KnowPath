"""Run lifecycle and SSE shared by the memory and SQL repositories.

Repository mutations commit a run and its events together. Cancellation is a
request; an execution boundary must acknowledge it before the run is cancelled.
"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Protocol
from uuid import uuid4

from .errors import DomainConflict, DomainNotFound, EventHistoryExpired

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
Run = dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunRepository(Protocol):
    def transaction(self): ...
    def create(self, run: Run) -> None: ...
    def get(self, run_id: str) -> Run: ...
    def mutate(self, run_id: str, change: Callable[[Run], None]) -> Run: ...
    def active_ids(self) -> list[str]: ...


class InMemoryRunRepository:
    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            previous = copy.deepcopy(self._runs)
            try:
                yield
            except BaseException:
                self._runs = previous
                raise

    def create(self, run: Run) -> None:
        with self._lock:
            if run["id"] in self._runs:
                raise DomainConflict("RUN_EXISTS", "任务已存在")
            self._runs[run["id"]] = copy.deepcopy(run)

    def get(self, run_id: str) -> Run:
        with self._lock:
            if run_id not in self._runs:
                raise DomainNotFound("run", run_id)
            return copy.deepcopy(self._runs[run_id])

    def mutate(self, run_id: str, change: Callable[[Run], None]) -> Run:
        with self._lock:
            run = self.get(run_id)
            change(run)
            self._runs[run_id] = copy.deepcopy(run)
            return run

    def active_ids(self) -> list[str]:
        with self._lock:
            return [key for key, run in self._runs.items() if run["status"] in ACTIVE_STATUSES]


class RunService:
    def __init__(self, repository: RunRepository | None = None, *, clock=utc_now):
        self.repository = repository if repository is not None else InMemoryRunRepository()
        self.clock = clock

    def _event(self, run: Run, name: str, data: dict[str, Any]) -> None:
        now = self.clock().isoformat()
        run["events"].append({
            "id": str(int(run["events"][-1]["id"]) + 1 if run["events"] else 1),
            "event": name,
            "created_at": now,
            "data": {**copy.deepcopy(data), "run_id": run["id"], "timestamp": now},
        })

    def create(self, kind: str, result_ref=None, *, status="queued") -> Run:
        if status not in {"queued", "running", "succeeded"}:
            raise DomainConflict("INVALID_RUN_STATUS", "任务初始状态不合法")
        if result_ref is not None and status != "succeeded":
            raise DomainConflict("INVALID_RUN_RESULT", "结果引用只能在任务成功时发布")
        now = self.clock().isoformat()
        run = {"id": str(uuid4()), "kind": kind, "status": status,
               "progress": 100 if status == "succeeded" else 0,
               "result_ref": copy.deepcopy(result_ref), "error": None,
               "created_at": now, "finished_at": now if status == "succeeded" else None,
               "events": []}
        if status != "queued":
            self._event(run, "run.started", {"kind": kind})
        if status == "succeeded":
            self._event(run, "run.completed", {"result_ref": result_ref})
        self.repository.create(run)
        return copy.deepcopy(run)

    def get(self, run_id: str) -> Run:
        return self.repository.get(run_id)

    def start(self, run_id: str) -> Run:
        def change(run):
            if run["status"] != "queued":
                raise DomainConflict("RUN_NOT_QUEUED", "任务不在等待状态")
            run["status"] = "running"
            self._event(run, "run.started", {"kind": run["kind"]})
        return self.repository.mutate(run_id, change)

    def append_event(self, run_id: str, name: str, data: dict[str, Any]) -> Run:
        # Lifecycle events must be emitted by the transitions below.
        if name not in {"progress", "tool.started", "tool.completed", "message.delta", "message.completed"}:
            raise DomainConflict("INVALID_RUN_EVENT", "事件类型不合法")
        if name == "progress":
            percent = data.get("percent")
            if type(percent) is not int or not 0 <= percent <= 100:
                raise DomainConflict("INVALID_RUN_PROGRESS", "进度须为 0 到 100 的整数")

        def change(run):
            self._require_running(run)
            if name == "progress":
                run["progress"] = max(run["progress"], data["percent"])
            self._event(run, name, data)
        return self.repository.mutate(run_id, change)

    @staticmethod
    def _require_running(run):
        if run["status"] == "cancelling":
            raise DomainConflict("RUN_CANCELLING", "任务正在取消，不能发布迟到结果")
        if run["status"] != "running":
            raise DomainConflict("RUN_NOT_RUNNING", "任务不在运行状态")

    def complete(self, run_id: str, result_ref=None) -> Run:
        def change(run):
            self._require_running(run)
            run.update(status="succeeded", progress=100,
                       result_ref=copy.deepcopy(result_ref), finished_at=self.clock().isoformat())
            self._event(run, "run.completed", {"result_ref": result_ref})
        return self.repository.mutate(run_id, change)

    def fail(self, run_id: str, error: dict[str, Any]) -> Run:
        def change(run):
            if run["status"] in TERMINAL_STATUSES:
                return
            run.update(status="failed", error=copy.deepcopy(error), finished_at=self.clock().isoformat())
            self._event(run, "run.failed", {"error": error})
        return self.repository.mutate(run_id, change)

    def request_cancel(self, run_id: str) -> Run:
        def change(run):
            if run["status"] in TERMINAL_STATUSES or run["status"] == "cancelling":
                return
            run["status"] = "cancelling"
            self._event(run, "run.cancelling", {})
        return self.repository.mutate(run_id, change)

    def acknowledge_cancel(self, run_id: str) -> Run:
        def change(run):
            if run["status"] in TERMINAL_STATUSES:
                return
            if run["status"] != "cancelling":
                raise DomainConflict("CANCEL_NOT_REQUESTED", "尚未请求取消")
            run.update(status="cancelled", finished_at=self.clock().isoformat())
            self._event(run, "run.cancelled", {})
        return self.repository.mutate(run_id, change)

    def events_for(self, run_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        if type(after_id) is not int or after_id < 0:
            raise DomainConflict("INVALID_EVENT_ID", "Last-Event-ID 须为非负整数")
        run = self.get(run_id)
        latest = int(run["events"][-1]["id"]) if run["events"] else 0
        if after_id > latest:
            raise DomainConflict("INVALID_EVENT_ID", "Last-Event-ID 超出任务事件范围")
        cutoff = self.clock() - timedelta(days=7)
        if any(e["data"] is not None and datetime.fromisoformat(e["created_at"]) < cutoff for e in run["events"]):
            # Retain only sequence/timestamp tombstones to detect expired cursors.
            def prune(current):
                for event in current["events"]:
                    if datetime.fromisoformat(event["created_at"]) < cutoff:
                        event["data"] = None
            run = self.repository.mutate(run_id, prune)
        events = [e for e in run["events"] if int(e["id"]) > after_id]
        if any(e["data"] is None for e in events):
            raise EventHistoryExpired(run_id)
        return events

    def recover_interrupted(self, *, exclude_kinds=(), exclude_ids=()) -> int:
        """Call once at startup, before admitting work, in the single-process runtime."""
        ids = [run_id for run_id in self.repository.active_ids()
               if run_id not in exclude_ids and self.get(run_id)["kind"] not in exclude_kinds]
        for run_id in ids:
            self.fail(run_id, {"code": "RUN_INTERRUPTED", "message": "服务重启，任务已中断",
                               "details": {}, "retryable": True})
        return len(ids)


async def stream_run_events(service: RunService, run_id: str, *, after_id=0,
                            is_disconnected=None, heartbeat_seconds=15.0, poll_seconds=0.25):
    """Poll committed events without blocking the ASGI event loop."""
    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + heartbeat_seconds
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        try:
            events = await asyncio.to_thread(service.events_for, run_id, after_id=after_id)
        except (DomainNotFound, EventHistoryExpired):
            # HTTP status is already sent; close so reconnect can return 404/410.
            return
        for event in events:
            after_id = int(event["id"])
            yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            if event["event"] in TERMINAL_EVENTS:
                return
        run = await asyncio.to_thread(service.get, run_id)
        latest = int(run["events"][-1]["id"]) if run["events"] else 0
        if run["status"] in TERMINAL_STATUSES:
            if after_id >= latest:
                return
            continue  # A terminal event committed between the two reads.
        if loop.time() >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = loop.time() + heartbeat_seconds
        await asyncio.sleep(poll_seconds)
