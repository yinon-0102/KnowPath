"""Durable write fences for content-v2 erasure; no network calls under SQL locks.

An intent has no automatic expiry: time passing cannot prove a remote request
stopped. Administrators may reconcile an orphan only after verifying its writer
has terminated. Pending deletion remains truthful across process crashes.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math

import sqlalchemy as sa

from ..persistence.db import Base
from ..persistence.rag_models import TABLES
from ..persistence.rag_repository import RagConflict, RagIntegrityError


def begin_rag_write(repository, manifest, material_id, collection):
    """Journal the destination while holding the same material lock as deletion."""
    with repository._transaction() as connection:
        material = repository._require(connection, Base.metadata.tables["materials"],
            {"id": material_id}, current=True)
        version = repository._require(connection, Base.metadata.tables["material_versions"],
            {"id": manifest["material_version_id"]}, current=True)
        if material["status"] == "archived" or version["material_id"] != material_id or version["status"] != "ready":
            raise RagIntegrityError("original material unavailable")
        current = repository._require(connection, TABLES["rag_manifests"],
            {"retrieval_version_id": manifest["retrieval_version_id"], "manifest_id": manifest["manifest_id"]}, current=True)["payload"]
        if current["attempt"] != manifest["attempt"] or current["status"] != "building":
            raise RagConflict("build no longer owns its write attempt")
        identifier = manifest["attempt"]
        values = dict(id=identifier, aggregate_type="material", aggregate_id=material_id,
            event_type="rag.index.write", status="processing", attempts=1,
            lease_token=None, lease_until=None, available_at=None,
            created_at=datetime.now(timezone.utc), payload=dict(material_id=material_id,
                material_version_id=version["id"], retrieval_version_id=manifest["retrieval_version_id"],
                collection=collection, attempt=identifier, embedding_calls=[]))
        connection.execute(Base.metadata.tables["outbox_events"].insert().values(**values))
        return identifier


def reserve_rag_embedding(repository, identifier, *, model, input_count):
    """Commit a call reservation before provider I/O; no text or credentials."""
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise ValueError("a bounded embedding model identifier is required")
    if type(input_count) is not int or not 1 <= input_count <= 10:
        raise ValueError("embedding audit expects a batch of one to ten inputs")
    outbox = Base.metadata.tables["outbox_events"]
    with repository._transaction() as connection:
        intent = repository._require(connection, outbox, {"id": identifier}, current=True)
        if intent["event_type"] != "rag.index.write" or intent["status"] != "processing":
            raise RagConflict("embedding reservation requires an active write intent")
        payload = deepcopy(intent["payload"])
        calls = payload.setdefault("embedding_calls", [])
        call_id = len(calls) + 1
        calls.append(dict(call_id=call_id, model=model, input_count=input_count, status="started",
            started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, elapsed_seconds=None,
            billing_unknown=True, usage={"calls": None, "input_tokens": None, "complete": False}))
        connection.execute(outbox.update().where(outbox.c.id == identifier).values(payload=payload))
        return call_id


def finish_rag_embedding(repository, identifier, call_id, *, elapsed_seconds, usage=None, failed=False):
    """Record safe counters only; missing/failed responses keep billing unknown."""
    if (type(call_id) is not int or call_id < 1 or type(failed) is not bool
            or type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0):
        raise ValueError("invalid embedding audit completion")
    # Never copy a provider mapping wholesale: custom implementations may attach
    # prompts, URLs, credentials, nonfinite values, or stale previous-call usage.
    raw = usage if isinstance(usage, dict) and not failed else {}
    def counter(key):
        value = raw.get(key)
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    safe = {"calls": counter("calls"), "input_tokens": counter("input_tokens")}
    safe["complete"] = raw.get("complete") is True and safe["input_tokens"] is not None
    outbox = Base.metadata.tables["outbox_events"]
    with repository._transaction() as connection:
        intent = repository._require(connection, outbox, {"id": identifier}, current=True)
        if intent["event_type"] != "rag.index.write" or intent["status"] != "processing":
            raise RagConflict("embedding completion requires an active write intent")
        payload = deepcopy(intent["payload"])
        calls = payload.get("embedding_calls", [])
        if call_id > len(calls) or calls[call_id - 1]["call_id"] != call_id or calls[call_id - 1]["status"] != "started":
            raise RagConflict("embedding call is not awaiting completion")
        record = calls[call_id - 1]
        record.update(status="failed" if failed else "succeeded", finished_at=datetime.now(timezone.utc).isoformat(),
                      elapsed_seconds=elapsed_seconds, usage=safe, billing_unknown=not safe["complete"])
        connection.execute(outbox.update().where(outbox.c.id == identifier).values(payload=payload))
        return deepcopy(record)


def capture_rag_cleanup(session, material_id):
    """Capture exact external identities before the material's SQL purge."""
    originals = Base.metadata.tables["material_versions"]
    versions = sorted(session.execute(sa.select(originals.c.id).where(
        originals.c.material_id == material_id).with_for_update()).scalars())
    retrievals = TABLES["rag_retrieval_versions"]
    rows = session.execute(sa.select(retrievals).where(
        retrievals.c.material_version_id.in_(versions)).with_for_update()).mappings().all()
    grouped = {}
    for row in rows:
        collection = row["profile"].get("vector_collection")
        if not collection:
            continue
        entry = grouped.setdefault(collection, {"material_version_ids": set(), "retrieval_version_ids": set()})
        entry["material_version_ids"].add(row["material_version_id"])
        entry["retrieval_version_ids"].add(row["retrieval_version_id"])
    outbox = Base.metadata.tables["outbox_events"]
    intents = session.execute(sa.select(outbox).where(outbox.c.event_type == "rag.index.write",
        outbox.c.aggregate_id == material_id).with_for_update()).mappings().all()
    # Intents preserve destinations even if a derivative was independently purged.
    for intent in intents:
        payload = intent["payload"]
        entry = grouped.setdefault(payload["collection"], {"material_version_ids": set(), "retrieval_version_ids": set()})
        entry["material_version_ids"].add(payload["material_version_id"])
        entry["retrieval_version_ids"].add(payload["retrieval_version_id"])
    indexes = [dict(collection=collection, **{key: sorted(value) for key, value in groups.items()})
               for collection, groups in sorted(grouped.items())]
    return dict(material_version_ids=versions, rag_indexes=indexes,
                rag_write_ids=sorted(intent["id"] for intent in intents if intent["status"] == "processing"))


def _close_write(repository, identifier, *, reconciliation=None):
    outbox = Base.metadata.tables["outbox_events"]
    with repository._transaction() as connection:
        intent = repository._require(connection, outbox, {"id": identifier}, current=True)
        if intent["event_type"] != "rag.index.write":
            raise RagIntegrityError("not a RAG write intent")
        if intent["status"] not in {"processing", "completed", "abandoned"}:
            raise RagConflict("invalid write intent state")
        material_id = intent["aggregate_id"]
        if intent["status"] == "processing":
            payload = deepcopy(intent["payload"])
            if reconciliation is not None:
                payload["reconciliation"] = reconciliation
            connection.execute(outbox.update().where(outbox.c.id == identifier).values(
                status="abandoned" if reconciliation is not None else "completed", payload=payload))
    # Separate transaction avoids inversion with deletion worker (event -> intent).
    # Always schedule a final sweep after closure: a previous sweep may have run
    # before the final remote write, even when compensation itself succeeded.
    with repository._transaction() as connection:
        events = connection.execute(sa.select(outbox).where(outbox.c.event_type == "material.delete",
            outbox.c.aggregate_id == material_id).with_for_update()).mappings().all()
        for event in events:
            if identifier not in event["payload"].get("rag_write_ids", []):
                continue
            connection.execute(outbox.update().where(outbox.c.id == event["id"]).values(
                status="pending", lease_token=None, lease_until=None, available_at=None))


def finish_rag_write(repository, identifier):
    """Called only when the writer will perform no further external operations."""
    _close_write(repository, identifier)


def reconcile_rag_write(repository, identifier, *, writer_stopped, reason):
    """Administrative entrypoint; confirmation must follow actual process fencing.

    This function is deliberately not an unauthenticated HTTP endpoint. An
    operator must stop the owning process/request first; a lease timeout is not
    sufficient. Exact cleanup remains the durable deletion worker's job.
    """
    if writer_stopped is not True:
        raise ValueError("explicit confirmation of writer stopped is required")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("a concise administrative reconciliation reason is required")
    _close_write(repository, identifier, reconciliation=dict(writer_stopped=True, reason=reason,
        confirmed_at=datetime.now(timezone.utc).isoformat()))
