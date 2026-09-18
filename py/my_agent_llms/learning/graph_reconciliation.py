"""Durable graph candidates; external preparation is a separate outbox consumer.

This stage preserves source-backed extraction and conservative content-change
conflicts. It never promotes a parsed material into a published graph.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from .errors import DomainConflict, DomainNotFound
from .spaces import SpaceService, now, topics_for_version


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def extract_snapshot(material_id, version):
    # This is a structural projection of parsed headings/chunks, not semantic
    # relation inference. Future extractors must provide validated source refs.
    # SQL does not guarantee source row order. Canonical document order must
    # precede node grouping, source refs and hashing (including tied positions).
    ordered = sorted(version.chunks, key=lambda chunk: (
        chunk.page if chunk.page is not None else -1,
        chunk.line_start if chunk.line_start is not None else -1,
        chunk.line_end if chunk.line_end is not None else -1,
        tuple(chunk.section_path), chunk.content_hash, chunk.id))
    version = replace(version, chunks=ordered)
    chunks = {chunk.id: chunk for chunk in version.chunks}
    nodes = topics_for_version(material_id, version)
    for node in nodes:
        node.pop("graph_version", None)
        node["content_hash"] = digest([chunks[ref["chunk_id"]].content_hash for ref in node["source_refs"]])
    sources = [{"id": chunk.id, "material_id": material_id, "material_version_id": version.id,
                "content_hash": chunk.content_hash, "section_path": list(chunk.section_path),
                "page": chunk.page, "line_start": chunk.line_start, "line_end": chunk.line_end}
               for chunk in version.chunks]
    return {"schema_version": 1, "material_id": material_id, "material_version_id": version.id,
            "nodes": nodes, "relations": [], "sources": sources}


def compare_snapshots(base, candidate):
    result = {"added": [], "changed": [], "removed": [], "unchanged": [], "conflicts": []}
    affected = set()
    for field, kind in (("nodes", "node"), ("relations", "relation"), ("sources", "source")):
        old = {row["id"]: row for row in base.get(field, [])}
        new = {row["id"]: row for row in candidate[field]}
        for identifier in sorted(old.keys() | new.keys()):
            before, after = old.get(identifier), new.get(identifier)
            category = "added" if before is None else "removed" if after is None else "unchanged" if before == after else "changed"
            result[category].append({"kind": kind, "id": identifier, "before": before, "after": after})
            if category != "unchanged" and kind == "node":
                affected.add(identifier)
            if kind == "node" and before and after and before["content_hash"] != after["content_hash"]:
                result["conflicts"].append({"conflict_id": "conflict_" + digest([identifier, before, after])[:32],
                    "kind": "content_change", "topic_id": identifier, "status": "pending",
                    "before": before, "after": after})
    result["affected_topic_ids"] = sorted(affected)
    return result


class GraphReconciliationService:
    def __init__(self, repository, materials, runs):
        self.repository, self.materials, self.runs = repository, materials, runs
        self.commands = SpaceService(repository, materials)

    def _material(self, material_id):
        material = self.materials.get_material(material_id)
        if material is None:
            raise DomainNotFound("material", material_id)
        return material

    def _history(self, material_id):
        return sorted(self.repository.records("graph_revisions", material_id=material_id), key=lambda row: row["sequence"])

    @staticmethod
    def _published(history):
        published = [row for row in history if row["status"] == "published"]
        return max(published, key=lambda row: row["graph_version"]) if published else None

    @staticmethod
    def _response(revision):
        return {"run_id": revision["run_id"], "status": "queued", "candidate_revision_id": revision["id"]}

    def reconcile(self, material_id, payload, key):
        def change():
            self._material(material_id)  # Material lock serializes candidate sequence and publication base.
            version = self.materials.get_version(payload["version_id"])
            if version is None or version.material_id != material_id:
                raise DomainNotFound("material_version", payload["version_id"])
            if version.status != "ready" or not version.chunks:
                raise DomainConflict("MATERIAL_NOT_READY", "资料版本尚未完成解析")
            history = self._history(material_id)
            published = self._published(history)
            base_version = published["graph_version"] if published else 0
            if payload["expected_graph_version"] != base_version:
                raise DomainConflict("VERSION_CONFLICT", "图谱版本已变化，请刷新后重试", {"graph_version": base_version})
            for row in reversed(history):
                if row["material_version_id"] == version.id and row["base_graph_version"] == base_version and row["status"] == "draft":
                    if self.runs.get(row["run_id"])["status"] in {"queued", "running"}:
                        return self._response(row)
            snapshot = extract_snapshot(material_id, version)
            revision_id = str(uuid4())
            run = self.runs.create("graph_reconcile")
            timestamp = now()
            revision = {"id": revision_id, "material_id": material_id, "material_version_id": version.id,
                "sequence": max((row["sequence"] for row in history), default=0) + 1,
                "base_graph_version": base_version, "graph_version": None, "status": "draft", "run_id": run["id"],
                "snapshot": snapshot, "snapshot_hash": digest(snapshot),
                "diff": compare_snapshots(published["snapshot"] if published else {}, snapshot), "created_at": timestamp}
            self.repository.put_record("graph_revisions", revision)
            self.repository.put_record("outbox", {"id": str(uuid4()), "aggregate_type": "graph_revision",
                "aggregate_id": revision_id, "event_type": "graph.prepare", "status": "pending", "created_at": timestamp,
                "payload": {"revision_id": revision_id, "material_id": material_id, "material_version_id": version.id,
                            "run_id": run["id"], "snapshot_hash": revision["snapshot_hash"]}})
            return self._response(revision)
        return self.commands._execute("graph.reconcile", material_id, payload, key, change)

    def diff(self, material_id, revision_id=None, include_unchanged=False):
        with self.repository.transaction():
            self._material(material_id)
            history = self._history(material_id)
            if revision_id:
                revision = next((row for row in history if row["id"] == revision_id), None)
                if revision is None:
                    raise DomainNotFound("graph_revision", revision_id)
            else:
                candidates = [row for row in history if row["status"] in {"draft", "pending_review"}]
                revision = candidates[-1] if candidates else None
            if revision is None:
                published = self._published(history)
                result = {"base_graph_version": published["graph_version"] if published else 0,
                          "candidate_revision_id": None, "added": [], "changed": [], "removed": [],
                          "conflicts": [], "affected_topic_ids": []}
                if include_unchanged:
                    result["unchanged"] = []
                return result
            result = copy.deepcopy(revision["diff"])
            if not include_unchanged:
                result.pop("unchanged", None)
            result.update(base_graph_version=revision["base_graph_version"], candidate_revision_id=revision["id"],
                          material_version_id=revision["material_version_id"], status=revision["status"])
            return result

    def publish(self, material_id, revision_id, payload, key):
        def change():
            self._material(material_id)
            history = self._history(material_id)
            revision = next((row for row in history if row["id"] == revision_id), None)
            if revision is None:
                raise DomainNotFound("graph_revision", revision_id)
            published = self._published(history)
            current = published["graph_version"] if published else 0
            if payload["expected_graph_version"] != current or revision["base_graph_version"] != current:
                raise DomainConflict("VERSION_CONFLICT", "图谱版本已变化，请重新生成候选快照")
            known = {item["conflict_id"] for item in revision["diff"]["conflicts"]}
            supplied = {item["conflict_id"] for item in payload.get("resolutions", [])}
            if supplied - known:
                raise DomainConflict("INVALID_CONFLICT_RESOLUTION", "冲突不属于该候选快照")
            # Readiness must be attested by the external graph/index worker.
            # Until that consumer and atomic publication are installed, fail
            # closed even if a caller knows a real candidate identifier.
            raise DomainConflict("REVISION_NOT_READY", "候选图谱和索引尚未准备就绪，当前不能发布", {"candidate_revision_id": revision_id})
        return self.commands._execute("graph.publish", material_id, {"revision_id": revision_id, **payload}, key, change)

    def delete_history(self, material_id):
        """Called inside material deletion; no queued work may outlive its sources."""
        with self.repository.transaction():
            self._material(material_id)
            history = self._history(material_id)
            for revision in history:
                self.runs.request_cancel(revision["run_id"])
                self.runs.acknowledge_cancel(revision["run_id"])
            self.repository.delete_history(material_id, {row["id"] for row in history})
