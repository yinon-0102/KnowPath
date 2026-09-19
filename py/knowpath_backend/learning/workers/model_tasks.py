"""Durable, bounded model jobs with lease-fenced publication.

Only assessment.generate and message.generate events are consumed. Model I/O is
outside transactions; each publication checks the lease inside its transaction.
"""
from contextlib import contextmanager
from contextvars import Context
from math import isfinite
from threading import Event, Thread
from time import monotonic as monotonic_time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from knowpath_backend.learning.errors import DomainNotFound

EVENTS = {"assessment.generate": "assessments", "message.generate": "messages"}
TRANSIENT_ERRORS = frozenset({"MODEL_UNAVAILABLE", "EMBEDDING_UNAVAILABLE", "VECTOR_UNAVAILABLE", "VECTOR_INDEX_NOT_READY", "RATE_LIMITED"})


def enqueue(repository, kind, resource):
    event = {"id": str(uuid4()), "event_type": kind + ".generate",
             "aggregate_type": kind, "aggregate_id": resource["id"],
             "payload": {"space_id": resource["space_id"], "run_id": resource["run_id"]},
             "status": "pending", "attempts": 0, "lease_token": None,
             "lease_until": None, "available_at": None,
             "created_at": datetime.now(timezone.utc).isoformat()}
    repository.put_record("outbox", event)
    return event["id"]


def timestamp(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class LeaseLost(Exception):
    """Another worker owns publication; discard this executor's result."""


class ExecutionTimedOut(LeaseLost):
    """This attempt exhausted its bounded execution budget."""


class ModelLease:
    """Renew one owned event without acquiring domain/Run row locks.

    The monotonic budget bounds renewal even when a provider cannot be interrupted.
    Wall time remains the persisted lease clock so independent workers can reclaim.
    """
    def __init__(self, worker, event):
        self.worker, self.event = worker, event
        self.deadline = worker.monotonic() + worker.max_execution_seconds
        self.stopped, self.lost = Event(), Event()
        self.observed_cancel = False
        self.thread = None

    def remaining(self):
        return self.deadline - self.worker.monotonic()

    def check(self):
        if self.remaining() <= 0:
            raise ExecutionTimedOut()
        if self.stopped.is_set() or self.lost.is_set():
            raise LeaseLost()

    def _owned(self, event):
        return event["status"] == "processing" and event["lease_token"] == self.event["lease_token"]

    def _timeout(self, event):
        event["payload"]["last_error"] = {
            "code": "MODEL_TASK_TIMEOUT", "message": "Model generation exceeded its execution time limit.",
            "details": {}, "retryable": True}
        # Do not extend or resurrect this lease. A claimant will use the existing
        # bounded attempt count and retain this error when the budget is exhausted.
        self.worker.repository.put_record("outbox", event)

    def mark_timeout(self):
        if self.remaining() > 0:
            return
        try:
            with self.worker.repository.transaction():
                event = self.worker.repository.get_record("outbox", self.event["id"])
                if self._owned(event):
                    self._timeout(event)
        except DomainNotFound:
            pass

    def renew(self):
        if self.stopped.is_set() or self.lost.is_set():
            return False
        try:
            # Read cancellation outside the event transaction: Run reads inside
            # the UOW lock rows, which would invert the domain lock order.
            run = self.worker.runs.get(self.event["payload"]["run_id"])
            if run["status"] != "running":
                self.observed_cancel = run["status"] in {"cancelling", "cancelled"}
                self.lost.set()
                return False
            # The daemon runs in an empty Context, so this is always a separate
            # UOW/session, never a caller's publication/deletion transaction.
            with self.worker.repository.transaction():
                event = self.worker.repository.get_record("outbox", self.event["id"])
                # A blocked SELECT may return after close(); recheck after the lock.
                if self.stopped.is_set():
                    return False
                if not self._owned(event):
                    self.lost.set()
                    return False
                remaining = self.remaining()
                if remaining <= 0:
                    self._timeout(event)
                    self.lost.set()
                    return False
                instant = self.worker.clock()
                if timestamp(event["lease_until"]) <= instant:
                    self.lost.set()
                    return False
                event["lease_until"] = (instant + timedelta(seconds=min(self.worker.lease_seconds, remaining))).isoformat()
                self.worker.repository.put_record("outbox", event)
                return True
        except DomainNotFound:
            self.lost.set()
            return False

    def _loop(self):
        while not self.stopped.wait(min(self.worker.heartbeat_interval, max(0, self.remaining()))):
            try:
                if not self.renew():
                    return
            except Exception:
                # Storage errors can contain credentials. Stop renewing and
                # discard output; lease expiry allows a healthy worker to retry.
                self.lost.set()
                return

    def start(self):
        if not self.renew():
            self.check()
            raise LeaseLost()
        self.thread = Thread(target=lambda: Context().run(self._loop),
                             name="model-lease-" + self.event["id"], daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            # A provider/database call cannot be forcibly stopped. A delayed
            # tick rechecks stopped under its row lock and cannot renew afterward.
            self.thread.join(timeout=0.2)


class ModelJob:
    def __init__(self, worker, event, lease=None):
        self.worker, self.event, self.lease = worker, event, lease

    def check(self):
        if self.lease is not None:
            self.lease.check()
        event = self.worker.repository.get_record("outbox", self.event["id"])
        if self.lease is not None:
            self.lease.check()
        if (event["status"] != "processing" or event["lease_token"] != self.event["lease_token"]
                or timestamp(event["lease_until"]) <= self.worker.clock()):
            raise LeaseLost()
        return event

    def retry(self, error):
        event = self.check()
        if error["code"] not in TRANSIENT_ERRORS or event["attempts"] >= self.worker.max_attempts:
            return False
        event["payload"]["last_error"] = dict(error)
        event.update(status="pending", lease_token=None, lease_until=None,
                     available_at=(self.worker.clock() + timedelta(seconds=min(300, 2 ** min(event["attempts"], 9)))).isoformat())
        self.worker.repository.put_record("outbox", event)
        return True

    def settle(self):
        event = self.check()
        run = self.worker.runs.get(event["payload"]["run_id"])
        status = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled"}.get(run["status"])
        if status:
            if run.get("error"):
                event["payload"]["last_error"] = run["error"]
            event.update(status=status, lease_token=None, lease_until=None, available_at=None)
            self.worker.repository.put_record("outbox", event)


class ModelTaskWorker:
    def __init__(self, assessments=None, messages=None, *, clock=None, lease_seconds=300, max_attempts=3,
                 max_execution_seconds=1200, heartbeat_interval=None, monotonic=None):
        service = assessments or messages
        heartbeat_interval = lease_seconds / 3 if heartbeat_interval is None else heartbeat_interval
        if (service is None or not isfinite(lease_seconds) or lease_seconds <= 0 or max_attempts < 1
                or not isfinite(max_execution_seconds) or not 1 <= max_execution_seconds <= 3600
                or not isfinite(heartbeat_interval) or not 0 < heartbeat_interval <= lease_seconds / 3):
            raise ValueError("A service, positive lease/retry limit, 1-3600 second execution budget and bounded heartbeat interval are required")
        self.assessments, self.messages = assessments, messages
        self.repository, self.runs = service.repository, service.runs
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.max_execution_seconds, self.heartbeat_interval = max_execution_seconds, heartbeat_interval
        self.monotonic = monotonic or monotonic_time

    def _service(self, event):
        return self.assessments if event["event_type"] == "assessment.generate" else self.messages

    @contextmanager
    def _locked(self, initial):
        service = self._service(initial)
        with self.repository.transaction():
            # Keep domain lock order: assessment -> space; space -> message.
            table = EVENTS[initial["event_type"]]
            resource, missing = None, False
            try:
                if table == "assessments":
                    resource = self.repository.get_record(table, initial["aggregate_id"])
                    service.spaces.repository.get(resource["space_id"])
                else:
                    service.spaces.repository.get(initial["payload"]["space_id"])
                    resource = self.repository.get_record(table, initial["aggregate_id"])
            except DomainNotFound:
                missing = True
            event = self.repository.get_record("outbox", initial["id"])
            try:
                run = self.runs.get(event["payload"]["run_id"])
            except DomainNotFound:
                run, missing = None, True
            if missing:
                # Legacy/deferred erasure may leave an event after its owner or
                # Run disappeared. Settle it here rather than polling forever.
                if event["status"] in {"pending", "processing"}:
                    event.update(status="cancelled", lease_token=None, lease_until=None, available_at=None)
                    self.repository.put_record("outbox", event)
                    if run is not None:
                        self.runs.request_cancel(run["id"])
                        self.runs.acknowledge_cancel(run["id"])
                    if resource is not None and resource["status"] in {"pending", "generating"}:
                        resource["status"] = "cancelled"
                        self.repository.put_record(table, resource)
                resource = None
            yield event, resource

    def claim(self, identifier):
        try:
            initial = self.repository.get_record("outbox", identifier, lock=False)
            if initial["event_type"] not in EVENTS or self._service(initial) is None:
                return None
            with self._locked(initial) as (event, resource):
                if resource is None:
                    return None
                instant = self.clock()
                if event["status"] not in {"pending", "processing"}:
                    return None
                if event["status"] == "pending" and event.get("available_at") and timestamp(event["available_at"]) > instant:
                    return None
                if event["status"] == "processing" and event.get("lease_until") and timestamp(event["lease_until"]) > instant:
                    return None
                run = self.runs.get(event["payload"]["run_id"])
                if run["status"] == "cancelling":
                    run = self.runs.acknowledge_cancel(run["id"])
                if run["status"] in {"succeeded", "failed", "cancelled"}:
                    event.update(status={"succeeded": "completed", "failed": "failed", "cancelled": "cancelled"}[run["status"]], lease_token=None, lease_until=None)
                    if run.get("error"):
                        event["payload"]["last_error"] = run["error"]
                    if resource["status"] in {"pending", "generating"}:
                        resource["status"] = "cancelled" if run["status"] == "cancelled" else "failed"
                        self.repository.put_record(EVENTS[event["event_type"]], resource)
                    self.repository.put_record("outbox", event)
                    return None
                if event["attempts"] >= self.max_attempts:
                    error = event["payload"].get("last_error") or {"code": "MODEL_UNAVAILABLE", "message": "Model generation retry limit reached.", "details": {}, "retryable": True}
                    self.runs.fail(run["id"], error)
                    resource["status"] = "failed"
                    self.repository.put_record(EVENTS[event["event_type"]], resource)
                    event.update(status="failed", lease_token=None, lease_until=None)
                    event["payload"]["last_error"] = error
                    self.repository.put_record("outbox", event)
                    return None
                if run["status"] == "queued":
                    self.runs.start(run["id"])
                event.update(status="processing", attempts=event["attempts"] + 1,
                             lease_token=str(uuid4()), lease_until=(instant + timedelta(seconds=self.lease_seconds)).isoformat(), available_at=None)
                self.repository.put_record("outbox", event)
                return event
        except DomainNotFound:
            return None

    def _settle_cancelled(self, initial):
        with self._locked(initial) as (event, resource):
            if (resource is None or event["status"] != "processing"
                    or event["lease_token"] != initial["lease_token"]):
                return
            run = self.runs.get(event["payload"]["run_id"])
            if run["status"] == "cancelling":
                run = self.runs.acknowledge_cancel(run["id"])
            if run["status"] != "cancelled":
                return
            if resource["status"] in {"pending", "generating"}:
                resource["status"] = "cancelled"
                self.repository.put_record(EVENTS[event["event_type"]], resource)
            event.update(status="cancelled", lease_token=None, lease_until=None, available_at=None)
            self.repository.put_record("outbox", event)

    def execute(self, event):
        lease = ModelLease(self, event)
        job = ModelJob(self, event, lease)
        try:
            with self._locked(event) as (_, resource):
                if resource is None:
                    return
                job.check()
            lease.start()
            self._service(event).generate(event["aggregate_id"], job=job)
            # Domain success/failure settles atomically. Early cancellation still
            # needs settlement, while retry has already relinquished its lease.
            with self._locked(event) as (_, resource):
                if resource is not None:
                    job.settle()
        except ExecutionTimedOut:
            lease.mark_timeout()
        except LeaseLost:
            if lease.observed_cancel:
                try:
                    self._settle_cancelled(event)
                except DomainNotFound:
                    pass
        except DomainNotFound:
            return
        finally:
            lease.close()

    def run_once(self, identifier=None):
        candidates = [identifier] if identifier else [row["id"] for row in self.repository.records("outbox", lock=False)
            if row["event_type"] in EVENTS and row["status"] in {"pending", "processing"}]
        for candidate in candidates:
            event = self.claim(candidate)
            if event:
                self.execute(event)
                return True
        return False
