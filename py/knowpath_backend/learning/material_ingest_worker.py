"""Durable original-file parsing with fenced atomic graph handoff."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from .errors import DomainNotFound
from .materials import MaterialParser, MaterialParseError


class MaterialParseWorker:
    def __init__(self, graph, parser=None, *, clock=None, lease_seconds=300, max_attempts=5):
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("positive lease and attempt limit required")
        self.graph, self.repository = graph, graph.repository
        self.parser = parser or MaterialParser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts

    @contextmanager
    def _locked(self, event_id):
        peek = self.repository.get_record("outbox", event_id, lock=False)
        with self.repository.transaction():
            material = self.graph._material(peek["payload"]["material_id"])
            version = self.graph.materials.get_version(peek["aggregate_id"])
            if version is None:
                raise DomainNotFound("material_version", peek["aggregate_id"])
            event = self.repository.get_record("outbox", event_id)
            yield event, material, version

    def _status(self, material, version, status):
        version.status = status
        self.graph.materials.update_version(version)
        if material.current_version_id == version.id and material.status != "archived":
            material.status = status
            self.graph.materials.update_material(material)

    def _cancelled(self, event, material, version):
        run = self.graph.runs.get(event["payload"]["run_id"])
        if run["status"] == "cancelling":
            self.graph.runs.acknowledge_cancel(run["id"])
        if run["status"] in {"cancelling", "cancelled", "failed", "succeeded"}:
            event.update(status="cancelled", lease_token=None, lease_until=None)
            self.repository.put_record("outbox", event)
            # A replacement job may already own the version. Never reset it.
            other = any(row["id"] != event["id"] and row["status"] in {"pending", "processing"}
                        and self.graph.runs.get(row["payload"]["run_id"])["status"] in {"queued", "running"}
                        for row in self.repository.records("outbox", aggregate_id=version.id, event_type="material.parse"))
            if not other and version.status == "processing":
                self._status(material, version, "uploaded")
            return True
        return False

    def _owned(self, event, claimed):
        return (event["status"] == "processing" and event.get("lease_token") == claimed["lease_token"]
                and datetime.fromisoformat(event["lease_until"]) > self.clock())

    def claim(self, event_id):
        try:
            with self._locked(event_id) as (event, material, version):
                if event["event_type"] != "material.parse" or event["status"] not in {"pending", "processing"}:
                    return None
                timestamp = self.clock()
                if event.get("lease_until") and datetime.fromisoformat(event["lease_until"]) > timestamp:
                    return None
                if event.get("available_at") and datetime.fromisoformat(event["available_at"]) > timestamp:
                    return None
                if self._cancelled(event, material, version):
                    return None
                if event.get("attempts", 0) >= self.max_attempts:
                    self._fail(event, material, version, terminal=True)
                    return None
                if self.graph.runs.get(event["payload"]["run_id"])["status"] == "queued":
                    self.graph.runs.start(event["payload"]["run_id"])
                event.update(status="processing", lease_token=str(uuid4()),
                    lease_until=(timestamp + timedelta(seconds=self.lease_seconds)).isoformat(),
                    attempts=event.get("attempts", 0) + 1, available_at=None)
                self.repository.put_record("outbox", event)
                return copy.deepcopy(event)
        except DomainNotFound:
            return None

    def _fail(self, event, material, version, *, terminal=False, code="MATERIAL_PARSE_FAILED"):
        terminal = terminal or event["attempts"] >= self.max_attempts
        event.update(status="failed" if terminal else "pending", lease_token=None, lease_until=None,
            available_at=None if terminal else (self.clock() + timedelta(seconds=min(300, 2 ** min(event["attempts"], 8)))).isoformat())
        self.repository.put_record("outbox", event)
        if terminal:
            self._status(material, version, "failed")
            self.graph.runs.fail(event["payload"]["run_id"], {"code": code,
                "message": "资料解析失败，请检查原文件后重试", "details": {}, "retryable": code == "MATERIAL_PARSE_FAILED"})

    def execute(self, claimed):
        try:
            with self._locked(claimed["id"]) as (event, material, version):
                if not self._owned(event, claimed) or self._cancelled(event, material, version):
                    return False
                filename, content_hash = version.filename, version.content_hash
            # Loading and parsing the original never encloses a write transaction.
            content = self.graph.materials.get_raw(claimed["aggregate_id"])
            if content is None or sha256(content).hexdigest() != content_hash or content_hash != claimed["payload"]["content_hash"]:
                raise MaterialParseError("original unavailable", code="MATERIAL_SOURCE_MISSING")
            chunks = self.parser.parse(content, filename=filename)
            with self._locked(claimed["id"]) as (event, material, version):
                if not self._owned(event, claimed) or self._cancelled(event, material, version):
                    return False
                if version.content_hash != content_hash:
                    raise MaterialParseError("original changed", code="MATERIAL_SOURCE_MISSING")
                version.chunks = chunks
                self._status(material, version, "ready")
                published = self.graph._published(self.graph._history(material.id))
                self.graph._stage(material.id, {"version_id": version.id,
                    "expected_graph_version": published["graph_version"] if published else 0},
                    run_kind="material_ingest", run_id=event["payload"]["run_id"])
                event.update(status="completed", lease_token=None, lease_until=None)
                self.repository.put_record("outbox", event)
            return True
        except DomainNotFound:
            return False
        except Exception as exc:
            try:
                with self._locked(claimed["id"]) as (event, material, version):
                    if self._owned(event, claimed) and not self._cancelled(event, material, version):
                        safe_codes = {"EMPTY_MATERIAL", "ENCRYPTED_PDF", "SCANNED_PDF_UNSUPPORTED",
                                      "PDF_PAGE_LIMIT_EXCEEDED", "MATERIAL_PARSE_FAILED", "MATERIAL_SOURCE_MISSING"}
                        code = exc.code if isinstance(exc, MaterialParseError) and exc.code in safe_codes else "MATERIAL_PARSE_FAILED"
                        self._fail(event, material, version, terminal=isinstance(exc, MaterialParseError), code=code)
            except DomainNotFound:
                pass
            return False

    def run_once(self, event_id=None):
        events = ([{"id": event_id}] if event_id else self.repository.records("outbox", lock=False, event_type="material.parse"))
        for event in events:
            claimed = self.claim(event["id"])
            if claimed:
                self.execute(claimed)
                return True
        return False
