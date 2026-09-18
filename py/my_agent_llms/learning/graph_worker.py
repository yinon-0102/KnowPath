"""Fenced graph.prepare consumer; preparers separately fence external writes."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .errors import DomainNotFound, DomainConflict
from .graph_reconciliation import digest


class LeaseLost(Exception):
    pass


def preparation_manifest(revision):
    # Prepare both sides of every reviewable conflict. Publication selects only
    # from this immutable superset; no late external writes are required.
    manifest = {"revision_id": revision["id"], "snapshot_hash": revision["snapshot_hash"],
                "graph_version": revision["base_graph_version"] + 1,
                "snapshot": copy.deepcopy(revision["snapshot"]),
                "old_nodes": [copy.deepcopy(c["before"]) for c in revision["diff"]["conflicts"]]}
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def receipt_valid(revision, receipt):
    return (isinstance(receipt, dict)
            and receipt.get("manifest_hash") == preparation_manifest(revision)["manifest_hash"]
            and receipt.get("neo4j", {}).get("verified") is True
            and receipt.get("qdrant", {}).get("verified") is True)


class GraphWorker:
    def __init__(self, graph, preparer, *, clock=None, lease_seconds=300, max_attempts=5):
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("positive lease and attempt limit required")
        self.graph, self.repository, self.preparer = graph, graph.repository, preparer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts

    @contextmanager
    def _locked(self, event_id):
        # Match reconcile/delete/publish order: material -> revision -> outbox -> Run.
        peek = self.repository.get_record("outbox", event_id, lock=False)
        with self.repository.transaction():
            if peek["payload"].get("space_id"):
                self.repository.get(peek["payload"]["space_id"])
            self.graph._material(peek["payload"]["material_id"])
            revision = self.repository.get_record("graph_revisions", peek["aggregate_id"])
            event = self.repository.get_record("outbox", event_id)
            yield event, revision

    def _cancelled(self, event, revision):
        run = self.graph.runs.get(revision["run_id"])
        if run["status"] == "cancelling":
            self.graph.runs.acknowledge_cancel(run["id"])
        if run["status"] in {"cancelling", "cancelled", "failed", "succeeded"}:
            event.update(status="cancelled", lease_token=None, lease_until=None)
            self.repository.put_record("outbox", event)
            if event["payload"].get("correction_id") and run["status"] in {"cancelling", "cancelled"}:
                correction = self.repository.get_record("corrections", event["payload"]["correction_id"])
                correction["status"] = "cancelled"
                self.repository.put_record("corrections", correction)
            return True
        return False

    def _owned(self, event, claimed):
        return (event["status"] == "processing" and event.get("lease_token") == claimed["lease_token"]
                and datetime.fromisoformat(event["lease_until"]) > self.clock())

    def claim(self, event_id):
        try:
            with self._locked(event_id) as (event, revision):
                if event["event_type"] != "graph.prepare" or event["status"] not in {"pending", "processing"}:
                    return None
                timestamp = self.clock()
                if event.get("lease_until") and datetime.fromisoformat(event["lease_until"]) > timestamp:
                    return None
                if event.get("available_at") and datetime.fromisoformat(event["available_at"]) > timestamp:
                    return None
                if self._cancelled(event, revision):
                    return None
                if event.get("attempts", 0) >= self.max_attempts:
                    self._fail(event, revision, terminal=True)
                    return None
                if self.graph.runs.get(revision["run_id"])["status"] == "queued":
                    self.graph.runs.start(revision["run_id"])
                event.update(status="processing", lease_token=str(uuid4()),
                             lease_until=(timestamp + timedelta(seconds=self.lease_seconds)).isoformat(),
                             attempts=event.get("attempts", 0) + 1, available_at=None)
                self.repository.put_record("outbox", event)
                return copy.deepcopy(event)
        except DomainNotFound:
            return None  # Deletion won the material lock.

    def heartbeat(self, claimed):
        try:
            with self._locked(claimed["id"]) as (event, revision):
                if not self._owned(event, claimed):
                    return False
                if self._cancelled(event, revision):
                    return False
                event["lease_until"] = (self.clock() + timedelta(seconds=self.lease_seconds)).isoformat()
                # Journal the destination before I/O, including partially successful attempts.
                backend = getattr(getattr(self.preparer, "vectors", None), "backend", None)
                collection = getattr(backend, "collection", None)
                if collection:
                    event["payload"]["external_collections"] = sorted(set(
                        event["payload"].get("external_collections", [])) | {collection})
                self.repository.put_record("outbox", event)
                return True
        except DomainNotFound:
            return False

    def _fail(self, event, revision, *, terminal=False, error_code="GRAPH_PREPARATION_FAILED"):
        terminal = terminal or event["attempts"] >= self.max_attempts
        event.update(status="failed" if terminal else "pending", lease_token=None, lease_until=None,
                     available_at=None if terminal else (self.clock() + timedelta(seconds=min(300, 2 ** min(event["attempts"], 8)))).isoformat())
        self.repository.put_record("outbox", event)
        if terminal:
            self.graph.runs.fail(revision["run_id"], {"code": error_code,
                "message": "图谱版本已变化，请重新提交纠错" if error_code == "VERSION_CONFLICT" else "图谱或向量索引准备失败，可重新提交任务", "details": {}, "retryable": error_code != "VERSION_CONFLICT"})
            if event["payload"].get("correction_id"):
                correction = self.repository.get_record("corrections", event["payload"]["correction_id"])
                correction["status"] = "failed"
                self.repository.put_record("corrections", correction)

    def execute(self, claimed):
        def heartbeat():
            if not self.heartbeat(claimed):
                raise LeaseLost()
        try:
            heartbeat()
            revision = self.repository.get_record("graph_revisions", claimed["aggregate_id"], lock=False)
            if digest(revision["snapshot"]) != revision["snapshot_hash"]:
                raise ValueError("invalid immutable snapshot")
            receipt = self.preparer.prepare(preparation_manifest(revision), heartbeat)
            if not receipt_valid(revision, receipt):
                raise ValueError("invalid preparation receipt")
            with self._locked(claimed["id"]) as (event, current):
                if not self._owned(event, claimed) or self._cancelled(event, current):
                    return False
                if not receipt_valid(current, receipt) or digest(current["snapshot"]) != current["snapshot_hash"]:
                    raise ValueError("snapshot changed during preparation")
                current.update(status="pending_review", preparation=receipt)
                event.update(status="completed", lease_token=None, lease_until=None)
                self.repository.put_record("graph_revisions", current)
                self.repository.put_record("outbox", event)
                result = {"candidate_revision_id": current["id"], "material_id": current["material_id"],
                          "material_version_id": current["material_version_id"]}
                correction_id = event['payload'].get('correction_id')
                if correction_id:
                    correction = self.repository.get_record('corrections', correction_id)
                    resolutions = [{'conflict_id': item['conflict_id'], 'action': 'use_new',
                                    'reason': correction['confirmation']['request']['reason']} for item in current['diff']['conflicts']]
                    result.update(self.graph.publish(current['material_id'], current['id'],
                        {'expected_graph_version': correction['base_graph_version'], 'resolutions': resolutions},
                        None, correction_id=correction_id))
                    result.update(correction_id=correction_id, affected_topic_ids=current['diff']['affected_topic_ids'], update_available=True)
                    correction.update(status='published', result=copy.deepcopy(result))
                    self.repository.put_record('corrections', correction)
                self.graph.runs.complete(current['run_id'], result)
            return True
        except (LeaseLost, DomainNotFound):
            return False
        except Exception as exc:
            # Never expose provider messages, URLs, credentials or source text.
            try:
                with self._locked(claimed["id"]) as (event, revision):
                    if self._owned(event, claimed) and not self._cancelled(event, revision):
                        stale = isinstance(exc, DomainConflict) and exc.code == "VERSION_CONFLICT"
                        self._fail(event, revision, terminal=stale, error_code="VERSION_CONFLICT" if stale else "GRAPH_PREPARATION_FAILED")
            except DomainNotFound:
                pass
            return False

    def run_once(self, event_id=None):
        candidates = ([{"id": event_id}] if event_id else
                      self.repository.records("outbox", lock=False, event_type="graph.prepare"))
        for candidate in candidates:
            claimed = self.claim(candidate["id"])
            if claimed:
                self.execute(claimed)
                return True
        return False
