"""Immutable SQL RAG snapshots. Publication lifecycle is deliberately separate.

Payloads are JSON dictionaries; keys and source identity are projected into SQL
columns with foreign keys. Exact retries are no-ops, never upserts. Each aggregate
and its ordered source spans commit together. All offsets are half-open.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import json
import re

import sqlalchemy as sa

from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.persistence.rag_models import TABLES


class RagConflict(ValueError):
    """An immutable identity was reused with different content."""


class RagIntegrityError(ValueError):
    """A RAG reference or source span cannot be proven valid."""


def _json_copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _spans(values):
    result = []
    for value in values:
        value = _json_copy(value)
        if set(value) - {"material_version_id", "artifact_hash", "start", "end", "page", "block"}:
            raise RagIntegrityError("unknown source span fields")
        start, end = value.get("start"), value.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise RagIntegrityError("source spans require nonempty half-open offsets")
        if not isinstance(value.get("material_version_id"), str) or not value["material_version_id"]:
            raise RagIntegrityError("material version identity is required")
        if not isinstance(value.get("artifact_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["artifact_hash"]):
            raise RagIntegrityError("artifact hash must be a SHA-256 digest")
        value.setdefault("page", None)
        value.setdefault("block", None)
        if value["page"] is not None and (type(value["page"]) is not int or value["page"] < 1):
            raise RagIntegrityError("page must be a positive integer or null")
        if value["block"] is not None and (not isinstance(value["block"], str) or not value["block"]):
            raise RagIntegrityError("block must be a nonempty string or null")
        result.append(value)
    return result


def _contained(source, allowed):
    identity = ("material_version_id", "artifact_hash", "page", "block")
    intervals = sorted((candidate["start"], candidate["end"]) for candidate in allowed
                       if all(source[key] == candidate[key] for key in identity))
    cursor = source["start"]
    for start, end in intervals:
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= source["end"]:
            return True
    return False


class SqlRagRepository:
    def __init__(self, engine):
        self.engine = engine

    @contextmanager
    def _transaction(self):
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "sqlite":
                # Python sqlite3 legacy transaction mode otherwise starts the
                # first SAVEPOINT outside a real transaction; RELEASE commits it.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _row(connection, table, key, *, current=False):
        query = sa.select(table).filter_by(**key)
        if current:
            query = query.with_for_update()
        return connection.execute(query).mappings().first()

    @classmethod
    def _require(cls, connection, table, key, *, current=False):
        row = cls._row(connection, table, key, current=current)
        if row is None:
            raise RagIntegrityError(f"missing {table.name} reference: {key}")
        return row

    @classmethod
    def _insert(cls, connection, table, values):
        key = {column.name: values[column.name] for column in table.primary_key}
        existing = cls._row(connection, table, key)
        if existing is None:
            try:
                # A duplicate insert may race another exact retry. The savepoint
                # keeps the outer aggregate transaction usable after that race.
                with connection.begin_nested():
                    connection.execute(table.insert().values(**values))
                return True
            except sa.exc.IntegrityError as error:
                existing = cls._row(connection, table, key, current=True)
                if existing is None:
                    raise RagIntegrityError(f"invalid {table.name} reference") from error
        if any(existing[name] != value for name, value in values.items()):
            raise RagConflict(f"immutable {table.name} identity already has different content: {key}")
        return False

    def _get(self, name, **key):
        with self.engine.connect() as connection:
            row = self._row(connection, TABLES[name], key)
            return deepcopy(row["payload"]) if row is not None else None

    def put_retrieval_version(self, payload):
        payload = _json_copy(payload)
        with self._transaction() as connection:
            self._require(connection, Base.metadata.tables["material_versions"], {"id": payload["material_version_id"]})
            self._insert(connection, TABLES["rag_retrieval_versions"], dict(
                retrieval_version_id=payload["retrieval_version_id"],
                material_version_id=payload["material_version_id"],
                profile_hash=payload["profile_hash"], profile=payload["profile"], payload=payload))

    def get_retrieval_version(self, retrieval_version_id):
        return self._get("rag_retrieval_versions", retrieval_version_id=retrieval_version_id)

    def put_chunk(self, payload, source_spans):
        payload = _json_copy(payload)
        spans = _spans(source_spans)
        if not spans or any(span["material_version_id"] != payload["material_version_id"] for span in spans):
            raise RagIntegrityError("a chunk requires source spans of its exact material version")
        payload["source_spans"] = spans
        key = {name: payload[name] for name in ("retrieval_version_id", "chunk_id")}
        with self._transaction() as connection:
            version = self._require(connection, TABLES["rag_retrieval_versions"], {"retrieval_version_id": key["retrieval_version_id"]})
            if version["material_version_id"] != payload["material_version_id"]:
                raise RagIntegrityError("chunk and retrieval version must bind the same material version")
            self._require(connection, Base.metadata.tables["material_versions"], {"id": payload["material_version_id"]})
            inserted = self._insert(connection, TABLES["rag_chunks"], dict(key,
                material_version_id=payload["material_version_id"], source_text=payload["source_text"], payload=payload))
            if inserted:
                connection.execute(TABLES["rag_chunk_spans"].insert(), [dict(key, ordinal=index, **span) for index, span in enumerate(spans)])

    def get_chunk(self, retrieval_version_id, chunk_id):
        return self._get("rag_chunks", retrieval_version_id=retrieval_version_id, chunk_id=chunk_id)

    @classmethod
    def _list_payloads(cls, connection, name, retrieval_version_id):
        table = TABLES[name]
        rows = connection.execute(sa.select(table).where(
            table.c.retrieval_version_id == retrieval_version_id).order_by(*table.primary_key)).mappings()
        return [deepcopy(row["payload"]) for row in rows]

    def list_chunks(self, retrieval_version_id):
        with self.engine.connect() as connection:
            return self._list_payloads(connection, "rag_chunks", retrieval_version_id)

    def list_nodes(self, retrieval_version_id):
        with self.engine.connect() as connection:
            return self._list_payloads(connection, "rag_nodes", retrieval_version_id)

    def get_manifest(self, retrieval_version_id, manifest_id):
        return self._get("rag_manifests", retrieval_version_id=retrieval_version_id, manifest_id=manifest_id)

    def put_node(self, payload):
        payload = _json_copy(payload)
        payload.setdefault("parent_node_id", None)
        payload.setdefault("chunk_id", None)
        key = {"retrieval_version_id": payload["retrieval_version_id"]}
        with self._transaction() as connection:
            self._require(connection, TABLES["rag_retrieval_versions"], key)
            if payload["parent_node_id"] is not None:
                self._require(connection, TABLES["rag_nodes"], dict(key, node_id=payload["parent_node_id"]))
            if payload["chunk_id"] is not None:
                self._require(connection, TABLES["rag_chunks"], dict(key, chunk_id=payload["chunk_id"]))
            self._insert(connection, TABLES["rag_nodes"], dict(key, node_id=payload["node_id"],
                parent_node_id=payload["parent_node_id"], chunk_id=payload["chunk_id"], kind=payload["kind"], payload=payload))

    def get_node(self, retrieval_version_id, node_id):
        return self._get("rag_nodes", retrieval_version_id=retrieval_version_id, node_id=node_id)

    def put_scope_snapshot(self, payload, allowed_spans):
        payload = _json_copy(payload)
        spans = _spans(allowed_spans)
        payload["allowed_spans"] = spans
        if type(payload["scope_version"]) is not int or payload["scope_version"] < 0:
            raise RagIntegrityError("scope_version must be nonnegative")
        with self._transaction() as connection:
            self._require(connection, Base.metadata.tables["learning_spaces"], {"id": payload["space_id"]})
            versions = set()
            # Lock original versions before any snapshot write, including scopes
            # with no spans (whose bindings otherwise exist only inside JSON).
            # Current reads reject a version deleted after this transaction's
            # consistent snapshot. Stable ordering avoids reversed lock orders
            # when scopes contain the same materials in different payload order.
            for binding in sorted(payload["bindings"], key=lambda item: item["material_version_id"]):
                version = self._require(connection, Base.metadata.tables["material_versions"],
                                        {"id": binding["material_version_id"]}, current=True)
                if version["material_id"] != binding["material_id"]:
                    raise RagIntegrityError("scope binding has mismatched material identity")
                if type(binding["graph_version"]) is not int or binding["graph_version"] < 1:
                    raise RagIntegrityError("graph_version must be positive")
                if version["id"] in versions:
                    raise RagIntegrityError("duplicate material version binding")
                versions.add(version["id"])
            if any(span["material_version_id"] not in versions for span in spans):
                raise RagIntegrityError("scope span is not part of a frozen binding")
            key = {"scope_snapshot_id": payload["scope_snapshot_id"]}
            inserted = self._insert(connection, TABLES["rag_scope_snapshots"], dict(key,
                space_id=payload["space_id"], scope_version=payload["scope_version"], bindings=payload["bindings"], payload=payload))
            if inserted and spans:
                connection.execute(TABLES["rag_scope_spans"].insert(), [dict(key, ordinal=index, **span) for index, span in enumerate(spans)])

    def get_scope_snapshot(self, scope_snapshot_id):
        return self._get("rag_scope_snapshots", scope_snapshot_id=scope_snapshot_id)

    @classmethod
    def _mapping_chunk(cls, connection, scope_snapshot_id, retrieval_version_id, chunk_id):
        scope = cls._require(connection, TABLES["rag_scope_snapshots"], {"scope_snapshot_id": scope_snapshot_id})["payload"]
        chunk = cls._require(connection, TABLES["rag_chunks"], dict(retrieval_version_id=retrieval_version_id, chunk_id=chunk_id))["payload"]
        if not chunk["source_spans"] or not all(_contained(span, scope["allowed_spans"]) for span in chunk["source_spans"]):
            raise RagIntegrityError("chunk source spans are outside the frozen scope")
        return chunk

    def put_scope_chunk_map(self, payload):
        if set(payload) != {"scope_snapshot_id", "retrieval_version_id", "chunk_id"}:
            raise RagIntegrityError("mapping requires exactly scope, retrieval version and chunk identities")
        with self._transaction() as connection:
            self._mapping_chunk(connection, **payload)
            self._insert(connection, TABLES["rag_scope_chunk_map"], dict(payload))

    def list_scope_chunks(self, scope_snapshot_id, retrieval_version_id):
        table = TABLES["rag_scope_chunk_map"]
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(table).where(
                table.c.scope_snapshot_id == scope_snapshot_id,
                table.c.retrieval_version_id == retrieval_version_id).order_by(table.c.chunk_id)).mappings().all()
            # Recheck containment on reads too: an out-of-band SQL mapping must
            # never turn an otherwise valid foreign key into a scope bypass.
            return [deepcopy(self._mapping_chunk(connection, **row)) for row in rows]
