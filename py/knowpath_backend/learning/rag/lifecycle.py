"""Verified manifests and compare-and-swap publication over immutable snapshots.

The validator is a trusted backend adapter, never a client supplied receipt. It
must read back both external indexes and return their exact chunk identities.
External calls run outside database transactions. Completion rechecks all SQL
identities and authorization under locks before committing readiness.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
from uuid import uuid4

import sqlalchemy as sa

from ..persistence.db import Base
from ..persistence.rag_models import TABLES
from ..persistence.rag_repository import RagConflict, RagIntegrityError, _contained, _json_copy


def _identities(values):
    result = []
    for item in values:
        if (not isinstance(item, dict) or set(item) != {"chunk_id", "content_hash"}
                or not isinstance(item["chunk_id"], str) or not item["chunk_id"]
                or not isinstance(item["content_hash"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["content_hash"])):
            raise RagIntegrityError("chunk identities require IDs and SHA-256 content hashes")
        result.append(dict(item))
    if len({item["chunk_id"] for item in result}) != len(result):
        raise RagIntegrityError("duplicate chunk identity")
    return sorted(result, key=lambda item: item["chunk_id"])


def _bindings(bindings):
    return sorted((b["material_id"], b["material_version_id"], b["graph_version"]) for b in bindings)


class ManifestLifecycle:
    def __init__(self, repository):
        self.repo = repository

    def _scope(self, connection, scope):
        frozen = self.repo._require(connection, TABLES["rag_scope_snapshots"],
            {"scope_snapshot_id": scope["scope_snapshot_id"]})["payload"]
        if frozen != scope:
            raise RagIntegrityError("scope must match its immutable SQL snapshot")
        space = self.repo._require(connection, Base.metadata.tables["learning_spaces"],
            {"id": scope["space_id"]}, current=True)
        if (space["status"] in {"archived", "deleted"} or space["scope_version"] != scope["scope_version"]
                or _bindings(space["bindings"]) != _bindings(scope["bindings"])):
            raise RagIntegrityError("scope is no longer current")
        for binding in sorted(scope["bindings"], key=lambda b: b["material_version_id"]):
            material = self.repo._require(connection, Base.metadata.tables["materials"],
                {"id": binding["material_id"]}, current=True)
            version = self.repo._require(connection, Base.metadata.tables["material_versions"],
                {"id": binding["material_version_id"]}, current=True)
            if (material["status"] != "ready" or version["status"] != "ready"
                    or version["material_id"] != material["id"]):
                raise RagIntegrityError("bound material is deleted, archived or unavailable")
        return frozen

    def _manifest(self, connection, rv, mid, *, current=False):
        return deepcopy(self.repo._require(connection, TABLES["rag_manifests"],
            dict(retrieval_version_id=rv, manifest_id=mid), current=current)["payload"])

    def _save(self, connection, manifest):
        table = TABLES["rag_manifests"]
        connection.execute(table.update().where(
            table.c.retrieval_version_id == manifest["retrieval_version_id"],
            table.c.manifest_id == manifest["manifest_id"]).values(
                status=manifest["status"], actual_counts=manifest["actual_counts"], payload=manifest))

    def start(self, retrieval_version_id, manifest_id, scope, expected_chunks,
              configuration, *, tree_version_id=None, retry=False):
        scope = _json_copy(scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope)
        expected = _identities(expected_chunks)
        if not expected:
            raise RagIntegrityError("manifest requires nonempty expected chunks")
        configuration = _json_copy(configuration)
        immutable = dict(retrieval_version_id=retrieval_version_id, manifest_id=manifest_id,
            scope_snapshot_id=scope["scope_snapshot_id"], space_id=scope["space_id"],
            expected_chunks=expected, configuration=configuration, tree_version_id=tree_version_id,
            expected_counts={"chunks": len(expected)},
            content_hash=sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        with self.repo._transaction() as connection:
            self._scope(connection, scope)
            version = self.repo._require(connection, TABLES["rag_retrieval_versions"],
                {"retrieval_version_id": retrieval_version_id}, current=True)
            if version["material_version_id"] not in {b["material_version_id"] for b in scope["bindings"]}:
                raise RagIntegrityError("retrieval version is not bound by this scope")
            immutable["material_version_id"] = version["material_version_id"]
            existing = self.repo._row(connection, TABLES["rag_manifests"],
                dict(retrieval_version_id=retrieval_version_id, manifest_id=manifest_id), current=True)
            if existing:
                payload = deepcopy(existing["payload"])
                if any(payload.get(k) != v for k, v in immutable.items()):
                    raise RagConflict("manifest configuration is immutable")
                if payload["status"] == "ready":
                    return payload
                if not retry:
                    raise RagConflict("existing build requires an explicit retry")
            payload = dict(immutable, attempt=uuid4().hex, status="building", a_ready=False,
                           b1_ready=False, actual_counts={}, failure=None)
            if existing:
                self._save(connection, payload)
            else:
                self.repo._insert(connection, TABLES["rag_manifests"], dict(
                    retrieval_version_id=retrieval_version_id, manifest_id=manifest_id,
                    scope_snapshot_id=scope["scope_snapshot_id"], space_id=scope["space_id"],
                    status="building", expected_counts=payload["expected_counts"], actual_counts={}, payload=payload))
            return deepcopy(payload)

    def _current(self, connection, rv, mid, attempt=None):
        initial = self._manifest(connection, rv, mid)
        scope = self.repo._require(connection, TABLES["rag_scope_snapshots"],
            {"scope_snapshot_id": initial["scope_snapshot_id"]})["payload"]
        self._scope(connection, scope)
        manifest = self._manifest(connection, rv, mid, current=True)
        if attempt is not None and manifest["attempt"] != attempt:
            raise RagConflict("outdated build attempt")
        return manifest, scope

    def _chunks(self, connection, manifest, scope):
        rv = manifest["retrieval_version_id"]
        self.repo._require(connection, TABLES["rag_retrieval_versions"],
            {"retrieval_version_id": rv}, current=True)
        table = TABLES["rag_chunks"]
        rows = connection.execute(sa.select(table).where(table.c.retrieval_version_id == rv)
                                  .with_for_update()).mappings().all()
        chunks, identities, allowed = [], [], set()
        for row in rows:
            chunk = deepcopy(row["payload"])
            if (chunk.get("chunk_id") != row["chunk_id"] or chunk.get("source_text") != row["source_text"]
                    or chunk.get("retrieval_version_id") != rv
                    or chunk.get("material_version_id") != manifest["material_version_id"]):
                raise RagIntegrityError("SQL chunk identity does not match its payload")
            text = chunk.get("retrieval_text", chunk["source_text"])
            if not isinstance(text, str) or not text:
                raise RagIntegrityError("retrieval text is empty")
            identities.append(dict(chunk_id=chunk["chunk_id"], content_hash=sha256(text.encode()).hexdigest()))
            spans = chunk.get("source_spans", [])
            if spans and all(_contained(span, scope["allowed_spans"]) for span in spans):
                allowed.add(chunk["chunk_id"])
            chunks.append(chunk)
        if _identities(identities) != manifest["expected_chunks"]:
            raise RagIntegrityError("SQL chunk content differs from immutable expected identities")
        mapping = TABLES["rag_scope_chunk_map"]
        mapped = set(connection.execute(sa.select(mapping.c.chunk_id).where(
            mapping.c.scope_snapshot_id == scope["scope_snapshot_id"], mapping.c.retrieval_version_id == rv)).scalars())
        if mapped != allowed:
            raise RagIntegrityError("scope mapping differs from the fully contained SQL chunks")
        return sorted(chunks, key=lambda chunk: chunk["chunk_id"])

    def mark_failed(self, retrieval_version_id, manifest_id, *, attempt, reason):
        with self.repo._transaction() as connection:
            manifest = self._manifest(connection, retrieval_version_id, manifest_id, current=True)
            if manifest["attempt"] != attempt:
                raise RagConflict("outdated build attempt")
            if manifest["status"] == "ready":
                raise RagConflict("ready manifests are immutable")
            manifest.update(status="failed", a_ready=False, b1_ready=False, failure=str(reason))
            self._save(connection, manifest)
            return manifest

    def _tree_ready(self, connection, manifest, receipts):
        receipt = receipts.get("tree", {})
        if (not manifest["tree_version_id"] or receipt.get("verified") is not True
                or receipt.get("tree_version_id") != manifest["tree_version_id"]):
            return False
        nodes = self.repo._list_payloads(connection, "rag_nodes", manifest["retrieval_version_id"])
        by_id = {node["node_id"]: node for node in nodes}
        chunks = {c['chunk_id']: c for c in self.repo._list_payloads(connection, 'rag_chunks', manifest['retrieval_version_id'])}
        roots = [node for node in nodes if node.get("parent_node_id") is None]
        if len(roots) != 1 or roots[0].get("chunk_id") is not None:
            return False
        leaves = set()
        parents = {node.get("parent_node_id") for node in nodes}
        for node in nodes:
            seen, current = set(), node
            while current.get("parent_node_id") is not None:
                if current["node_id"] in seen or current["parent_node_id"] not in by_id:
                    return False
                seen.add(current["node_id"])
                current = by_id[current["parent_node_id"]]
            if node.get("chunk_id") is not None:
                if node["kind"] != "leaf" or node["node_id"] in parents or node["chunk_id"] in leaves:
                    return False
                chunk = chunks.get(node['chunk_id'])
                if chunk is None:
                    return False
                if 'section_path' in chunk:
                    from .chunking import _identifier
                    path = chunk['section_path']
                    expected_parent = (_identifier('section', manifest['retrieval_version_id'],
                        manifest['material_version_id'], path) if path else None)
                    if chunk.get('parent_id') != expected_parent or node.get('parent_node_id') != (expected_parent or roots[0]['node_id']):
                        return False
                leaves.add(node["chunk_id"])
            elif node["node_id"] not in parents:
                return False
            elif node.get('kind') == 'section':
                from .chunking import _identifier
                path = node.get('section_path')
                if not path or node['node_id'] != _identifier('section', manifest['retrieval_version_id'], manifest['material_version_id'], path):
                    return False
                parent = (_identifier('section', manifest['retrieval_version_id'], manifest['material_version_id'], path[:-1])
                    if len(path) > 1 else roots[0]['node_id'])
                if node.get('parent_node_id') != parent:
                    return False
        return leaves == {item["chunk_id"] for item in manifest["expected_chunks"]}

    def validate(self, retrieval_version_id, manifest_id, validator, *, attempt):
        claimed = False
        try:
            with self.repo._transaction() as connection:
                manifest, scope = self._current(connection, retrieval_version_id, manifest_id, attempt)
                if manifest["status"] == "ready":
                    return manifest
                if manifest["status"] != "building":
                    raise RagConflict("validation requires a building attempt; retry explicitly")
                chunks = self._chunks(connection, manifest, scope)
                manifest["status"] = "validating"
                self._save(connection, manifest)
            claimed = True
            receipts = _json_copy(validator(deepcopy(manifest), deepcopy(chunks)))
            for channel in ("dense", "lexical"):
                receipt = receipts.get(channel, {})
                if (receipt.get("verified") is not True or "chunks" not in receipt
                        or _identities(receipt["chunks"]) != manifest["expected_chunks"]):
                    raise RagIntegrityError(f"{channel} index read-back verification failed")
            with self.repo._transaction() as connection:
                manifest, scope = self._current(connection, retrieval_version_id, manifest_id, attempt)
                if manifest["status"] != "validating":
                    raise RagConflict("attempt no longer validating")
                self._chunks(connection, manifest, scope)
                manifest.update(status="ready", a_ready=True,
                    b1_ready=self._tree_ready(connection, manifest, receipts),
                    actual_counts={"chunks": len(chunks), "dense": len(chunks), "lexical": len(chunks)},
                    verification=receipts)
                self._save(connection, manifest)
                return manifest
        except Exception as error:
            if isinstance(error, RagConflict) and not claimed:
                # A duplicate validator or stale retry must not cancel the owner.
                raise
            # Do not clobber a newer retry, a completed manifest, or a deletion.
            try:
                self.mark_failed(retrieval_version_id, manifest_id, attempt=attempt, reason="VALIDATION_FAILED")
            except (RagConflict, RagIntegrityError):
                pass
            raise

    def _ready_state(self, connection, retrieval_version_id, manifest_id):
        manifest, scope = self._current(connection, retrieval_version_id, manifest_id)
        if manifest["status"] != "ready" or manifest.get("a_ready") is not True:
            raise RagIntegrityError("manifest is not ready")
        chunks = self._chunks(connection, manifest, scope)
        count = len(chunks)
        if (manifest.get("expected_counts") != {"chunks": count}
                or manifest.get("actual_counts") != {"chunks": count, "dense": count, "lexical": count}):
            raise RagIntegrityError("manifest counts do not match complete SQL identities")
        if manifest.get("b1_ready") is True and not self._tree_ready(connection, manifest, manifest.get("verification", {})):
            raise RagIntegrityError("ready B1 tree no longer validates")
        return manifest, chunks

    def validate_ready(self, retrieval_version_id, manifest_id, validator):
        """Revalidate READY without rewriting it or accepting caller manifest data.

        Complete SQL identities/counts/scope/tree are checked on each side of the
        external readback. The callback executes outside database transactions.
        """
        with self.repo._transaction() as connection:
            manifest, chunks = self._ready_state(connection, retrieval_version_id, manifest_id)
        receipts = _json_copy(validator(deepcopy(manifest), deepcopy(chunks)))
        for channel in ("dense", "lexical"):
            receipt = receipts.get(channel, {})
            if (receipt.get("verified") is not True or "chunks" not in receipt
                    or _identities(receipt["chunks"]) != manifest["expected_chunks"]):
                raise RagIntegrityError(f"{channel} index read-back verification failed")
        with self.repo._transaction() as connection:
            current, _ = self._ready_state(connection, retrieval_version_id, manifest_id)
            if current != manifest:
                raise RagConflict("ready manifest changed during validation")
        if manifest.get("b1_ready") is True:
            receipts["tree"] = {"verified": True, "tree_version_id": manifest["tree_version_id"]}
        return receipts

    def publish(self, retrieval_version_id, manifest_id, expected_generation):
        if type(expected_generation) is not int or expected_generation < 0:
            raise RagIntegrityError("expected publication generation must be nonnegative")
        with self.repo._transaction() as connection:
            manifest, scope = self._current(connection, retrieval_version_id, manifest_id)
            if manifest["status"] != "ready" or manifest.get("a_ready") is not True:
                raise RagIntegrityError("only verified ready manifests may be published")
            self._chunks(connection, manifest, scope)
            table = TABLES["rag_publications"]
            key = dict(space_id=manifest["space_id"], material_version_id=manifest["material_version_id"])
            old = self.repo._row(connection, table, key, current=True)
            actual = old["generation"] if old else 0
            if actual != expected_generation:
                raise RagConflict("publication generation changed")
            values = dict(key, retrieval_version_id=retrieval_version_id, manifest_id=manifest_id,
                          scope_snapshot_id=manifest["scope_snapshot_id"], generation=actual + 1)
            if old:
                connection.execute(table.update().filter_by(**key).where(table.c.generation == actual).values(**values))
            else:
                connection.execute(table.insert().values(**values))
            return actual + 1

    def pin(self, scope, *, require_b1=False):
        scope = _json_copy(scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope)
        with self.repo._transaction() as connection:
            self._scope(connection, scope)
            table = TABLES["rag_publications"]
            rows = connection.execute(sa.select(table).where(table.c.space_id == scope["space_id"])
                .order_by(table.c.material_version_id).with_for_update()).mappings().all()
            expected_versions = {b["material_version_id"] for b in scope["bindings"]}
            if {row["material_version_id"] for row in rows} != expected_versions:
                raise RagIntegrityError("publication set does not cover exactly the current bound versions")
            result = []
            for row in rows:
                if row["scope_snapshot_id"] != scope["scope_snapshot_id"]:
                    raise RagIntegrityError("mixed or outdated scope publication set")
                manifest = self._manifest(connection, row["retrieval_version_id"], row["manifest_id"], current=True)
                if (manifest["status"] != "ready" or manifest.get("a_ready") is not True
                        or (require_b1 and manifest.get("b1_ready") is not True)):
                    raise RagIntegrityError("required manifest channel is unavailable")
                self._chunks(connection, manifest, scope)
                result.append(dict(manifest, generation=row["generation"]))
            return result
