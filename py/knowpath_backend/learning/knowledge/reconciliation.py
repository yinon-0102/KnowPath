"""Durable graph candidates, reviewed publication and pinned snapshot reads."""
from __future__ import annotations

from knowpath_backend.observability.events import origin_fields

import copy
import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.spaces.service import SpaceService, now, topics_for_version
from knowpath_backend.learning.knowledge.relations import extract_relations, confirm_grounded_relations


def digest(value):
    def restore_confidence(item):
        if isinstance(item, dict):
            # Producers emit float confidence. MySQL JSON stores integral
            # floats as ints; restore only this schema field so persisted
            # snapshots/manifests retain their legacy hash bytes. Exclude bool.
            return {key: float(child) if key == "confidence" and type(child) is int and child in (0, 1)
                    else restore_confidence(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [restore_confidence(child) for child in item]
        return item

    return sha256(json.dumps(restore_confidence(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def extract_snapshot(material_id, version):
    # Structural hierarchy and explicit source declarations are deterministic.
    # No model/network call runs inside the graph staging SQL transaction.
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
            "nodes": nodes, "relations": extract_relations(material_id, version, nodes), "sources": sources}


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
            if category != "unchanged" and kind == "relation":
                for side in (before, after):
                    if side:
                        affected.update(side[key] for key in ("from_id", "to_id"))
            from .learning_units import SEMANTIC_FIELDS
            reviewed_fields = ("name", "description", "status", "automatic_questions", "prerequisites", "alternatives") + SEMANTIC_FIELDS
            if kind == "node" and before and after and (
                    before["content_hash"] != after["content_hash"]
                    or any(before.get(field) != after.get(field) for field in reviewed_fields)):
                result["conflicts"].append({"conflict_id": "conflict_" + digest([identifier, before, after])[:32],
                    "kind": "content_change" if before["content_hash"] != after["content_hash"] else "reviewed_change",
                    "topic_id": identifier, "status": "pending",
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

    def _lock_work(self, material_id):
        """Acquire the aggregate's existing work before any Run lock.

        Commands may cancel several revisions or a parser handoff. Prelocking
        keeps those later transitions in material -> version/revision -> outbox
        -> Run order, including calls nested inside a worker transaction.
        """
        material = self._material(material_id)
        versions = sorted(self.materials.list_versions(material_id), key=lambda v: v.id)
        for version in versions:
            self.materials.get_version(version.id)
        history = self._history(material_id)
        for aggregate_id in sorted([v.id for v in versions] + [r['id'] for r in history]):
            self.repository.records('outbox', aggregate_id=aggregate_id)
        return material

    @staticmethod
    def _published(history):
        published = [row for row in history if row["status"] == "published"]
        return max(published, key=lambda row: row["graph_version"]) if published else None

    def _response(self, revision):
        return {"run_id": revision["run_id"], "status": self.runs.get(revision["run_id"])["status"],
                "candidate_revision_id": revision["id"], "material_version_id": revision["material_version_id"]}

    def _invalidate(self, history, *, base=None, except_id=None, cancel_parses=False, material_id=None, keep_version=None):
        """Caller holds material lock. Fence revisions before outbox and Runs."""
        obsolete = [r for r in history if r['id'] != except_id and r['status'] in {'draft', 'pending_review'}
                    and (base is None or r['base_graph_version'] == base)]
        versions = []
        if cancel_parses:
            for version in sorted(self.materials.list_versions(material_id), key=lambda v: v.id):
                if version.id != keep_version:
                    versions.append(self.materials.get_version(version.id))
        for revision in obsolete:
            revision['status'] = 'superseded'
            self.repository.put_record('graph_revisions', revision)
        for revision in obsolete:
            for event in self.repository.records('outbox', aggregate_id=revision['id']):
                if event['status'] in {'pending', 'processing'}:
                    event.update(status='cancelled', lease_token=None, lease_until=None)
                    self.repository.put_record('outbox', event)
        parse_runs = []
        for version in versions:
            for event in self.repository.records('outbox', aggregate_id=version.id, event_type='material.parse'):
                if event['status'] in {'pending', 'processing'}:
                    event.update(status='cancelled', lease_token=None, lease_until=None)
                    self.repository.put_record('outbox', event)
                    parse_runs.append(event['payload']['run_id'])
        for run_id in [r['run_id'] for r in obsolete] + parse_runs:
            self.runs.request_cancel(run_id)
            self.runs.acknowledge_cancel(run_id)

    def lifecycle(self, material_id, version_id=None):
        from knowpath_backend.learning.materials.service import MaterialParser
        with self.repository.transaction():
            material = self._lock_work(material_id)
            version = self.materials.get_version(version_id or material.current_version_id)
            if version is None or version.material_id != material_id:
                raise DomainNotFound('material_version', version_id)
            history = self._history(material_id)
            published = self._published(history)
            revisions = [r for r in history if r['material_version_id'] == version.id and r['status'] != 'superseded']
            candidate = revisions[-1] if revisions else None
            historical = [r for r in history if r['material_version_id'] == version.id and r.get('publication')]
            if candidate is None and historical:
                candidate = max(historical, key=lambda r: r['graph_version'])
            parse_events = self.repository.records('outbox', aggregate_id=version.id, event_type='material.parse')
            parse_runs = [self.runs.get(e['payload']['run_id']) for e in parse_events]
            run = self.runs.get(candidate['run_id']) if candidate else max(parse_runs, key=lambda r: r['created_at'], default=None)
            stage = ('learnable' if candidate and candidate.get('publication') else
                     'pending_review' if candidate and candidate['status'] == 'pending_review' else
                     'preparing' if candidate else 'pending_review' if version.chunks else
                     'parsing' if run and run['status'] in {'queued', 'running'} else 'received')
            error = None
            if (candidate and candidate['status'] == 'failed') or (run and run['status'] in {'failed', 'cancelled', 'cancelling'}) or version.status == 'failed':
                stage = 'failed'
                error = copy.deepcopy(run.get('error')) if run else None
                error = error or {'code': 'MATERIAL_TASK_CANCELLED' if run and run['status'] in {'cancelled', 'cancelling'} else 'MATERIAL_IMPORT_FAILED',
                    'message': '任务已取消，可重新提交任务' if run and run['status'] in {'cancelled', 'cancelling'} else '资料处理失败，可重新提交任务',
                    'details': {'stage': 'preparing' if version.chunks else 'parsing', 'retryable': True}, 'retryable': True}
            return {'stage': stage, 'material_version_id': version.id, 'parser_version': version.parser_version,
                    'current_parser_version': MaterialParser.VERSION,
                    'parser_upgrade_available': bool(version.chunks) and version.parser_version != MaterialParser.VERSION,
                    'parse_manifest': copy.deepcopy(version.parse_manifest),
                    'next_action': ('retry' if error and error.get('retryable') else 'reparse') if error else
                                   'reconcile' if stage == 'pending_review' and not candidate else
                                   'publish' if stage == 'pending_review' else 'ingest' if stage == 'received' else None,
                    'candidate_revision_id': candidate['id'] if candidate else None, 'run_id': run['id'] if run else None,
                    'error': error, 'published_available': published is not None,
                    'published_revision_id': published['id'] if published else None,
                    'published_graph_version': published['graph_version'] if published else None}

    def retry(self, material_id, payload, key):
        def change():
            self._lock_work(material_id)
            facts = self.lifecycle(material_id, payload['version_id'])
            if facts['stage'] != 'failed':
                raise DomainConflict('TASK_NOT_FAILED', '当前任务没有失败，请查看处理状态')
            if not facts['error'].get('retryable', False):
                raise DomainConflict('TASK_NOT_RETRYABLE', '请检查原文件，上传修正版本或重新解析')
            self._invalidate(self._history(material_id), cancel_parses=True, material_id=material_id)
            return self.enqueue_ingest(material_id, payload['version_id'])
        return self.commands._execute('material.retry', material_id, payload, key, change)

    def reparse(self, material_id, payload, key):
        from datetime import datetime, timezone
        from knowpath_backend.learning.materials.service import MaterialParser, MaterialVersion
        parser_version = MaterialParser.VERSION
        def change():
            material = self._lock_work(material_id)
            source = self.materials.get_version(payload['version_id'])
            if source is None or source.material_id != material_id:
                raise DomainNotFound('material_version', payload['version_id'])
            content = self.materials.get_raw(source.id)
            if content is None or sha256(content).hexdigest() != source.content_hash:
                raise DomainConflict('MATERIAL_SOURCE_MISSING', '原文件缺失或校验失败，请重新上传')
            timestamp = datetime.now(timezone.utc)
            version = MaterialVersion(id=str(uuid4()), material_id=material_id, filename=source.filename,
                media_type=source.media_type, content_hash=source.content_hash, size_bytes=source.size_bytes,
                status='uploaded', created_at=timestamp, parser_version=parser_version)
            self._invalidate(self._history(material_id), cancel_parses=True, material_id=material_id)
            material.current_version_id, material.updated_at = version.id, timestamp
            material.size_bytes = version.size_bytes
            material.version += 1
            if material.status != 'archived':
                material.status = 'uploaded'
            self.materials.save(material, version, idempotency_key='reparse-source:'+version.id,
                request_fingerprint=digest([source.id, parser_version]))
            self.materials.save_raw(version.id, content)
            return {**self.enqueue_ingest(material_id, version.id), 'material_version_id': version.id}
        return self.commands._execute('material.reparse', material_id,
            {**payload, 'parser_version': parser_version}, key, change)

    def rebuild(self, material_id, payload, key):
        def change():
            self._lock_work(material_id)
            self._invalidate(self._history(material_id), base=payload['expected_graph_version'])
            return self._stage(material_id, payload)
        return self.commands._execute('graph.rebuild', material_id, payload, key, change)

    def ingest(self, material_id, payload, key):
        """Queue saved originals or already parsed sources, without publishing."""
        return self.commands._execute("material.ingest", material_id, payload, key,
                                      lambda: self.enqueue_ingest(material_id, payload["version_id"]))

    def enqueue_ingest(self, material_id, version_id):
        # Called within the command/upload transaction; never retry a nested UOW.
        material = self._lock_work(material_id)
        version = self.materials.get_version(version_id)
        if version is None or version.material_id != material_id:
            raise DomainNotFound("material_version", version_id)
        if material.current_version_id != version_id:
            raise DomainConflict('MATERIAL_VERSION_STALE', '历史资料版本只读，请从原文件重新解析生成新版本')
        if version.status == "ready" and version.chunks:
            published = self._published(self._history(material_id))
            return self._stage(material_id, {"version_id": version_id,
                "expected_graph_version": published["graph_version"] if published else 0}, run_kind="material_ingest")
        if self.materials.get_raw(version_id) is None:
            raise DomainConflict("MATERIAL_SOURCE_MISSING", "原文件不存在，请重新上传资料")
        self._invalidate(self._history(material_id), cancel_parses=True, material_id=material_id, keep_version=version_id)
        for event in self.repository.records("outbox", aggregate_type="material_version", aggregate_id=version_id,
                                             event_type="material.parse"):
            if event["status"] in {"pending", "processing"}:
                run = self.runs.get(event["payload"]["run_id"])
                if run["status"] in {"queued", "running"}:
                    return {"run_id": run["id"], "status": run['status'], "candidate_revision_id": None}
        run = self.runs.create("material_ingest")
        version.status = "processing"
        self.materials.update_version(version)
        if material.current_version_id == version_id and material.status != "archived":
            material.status = "processing"
            self.materials.update_material(material)
        self.repository.put_record("outbox", {"id": str(uuid4()), "aggregate_type": "material_version",
            "aggregate_id": version_id, "event_type": "material.parse", "status": "pending", "created_at": now(),
            "payload": {"material_id": material_id, "material_version_id": version_id,
                        "run_id": run["id"], **origin_fields(), "content_hash": version.content_hash}})
        return {"run_id": run["id"], "status": "queued", "candidate_revision_id": None}

    def reconcile(self, material_id, payload, key):
        return self.commands._execute("graph.reconcile", material_id, payload, key,
                                      lambda: self._stage(material_id, payload))

    def _stage(self, material_id, payload, *, run_kind="graph_reconcile", run_id=None):
        # Caller owns the transaction and retry boundary. Retrying a nested
        # transaction would reuse a failed Session or commit partial staging.
        material = self._lock_work(material_id)
        version = self.materials.get_version(payload["version_id"])
        if version is None or version.material_id != material_id:
            raise DomainNotFound("material_version", payload["version_id"])
        if material.current_version_id != version.id:
            raise DomainConflict('MATERIAL_VERSION_STALE', '历史解析版本只读，请重新解析为新版本')
        if version.status != "ready" or not version.chunks:
            raise DomainConflict("MATERIAL_NOT_READY", "资料版本尚未完成解析")
        history = self._history(material_id)
        published = self._published(history)
        base_version = published["graph_version"] if published else 0
        if payload["expected_graph_version"] != base_version:
            raise DomainConflict("VERSION_CONFLICT", "图谱版本已变化，请刷新后重试", {"graph_version": base_version})
        # Rebuilding an immutable material version must preserve prior review
        # decisions, including corrections and keep_old/keep_both publication.
        snapshot = (copy.deepcopy(published["publication"]["snapshot"])
                    if published and published["material_version_id"] == version.id
                    else extract_snapshot(material_id, version))
        for row in reversed(history):
            if (run_id is None and row['material_version_id'] == version.id and row['base_graph_version'] == base_version
                    and row['snapshot_hash'] == digest(snapshot) and row['status'] in {'draft', 'pending_review'}
                    and not row['diff'].get('correction_id')
                    and self.runs.get(row['run_id'])['status'] in {'queued', 'running', 'succeeded'}):
                return self._response(row)
        self._invalidate(history, base=base_version)
        revision_id = str(uuid4())
        run = self.runs.get(run_id) if run_id else self.runs.create(run_kind)
        timestamp = now()
        revision = {"id": revision_id, "material_id": material_id, "material_version_id": version.id,
            "sequence": max((row["sequence"] for row in history), default=0) + 1,
            "base_graph_version": base_version, "graph_version": None, "status": "draft", "run_id": run["id"],
            "snapshot": snapshot, "snapshot_hash": digest(snapshot),
            "diff": compare_snapshots(published["publication"]["snapshot"] if published and published.get("publication") else published["snapshot"] if published else {}, snapshot), "created_at": timestamp}
        self.repository.put_record("graph_revisions", revision)
        self.repository.put_record("outbox", {"id": str(uuid4()), "aggregate_type": "graph_revision",
            "aggregate_id": revision_id, "event_type": "graph.prepare", "status": "pending", "created_at": timestamp,
            "payload": {"revision_id": revision_id, "material_id": material_id, "material_version_id": version.id,
                        "run_id": run["id"], **origin_fields(), "snapshot_hash": revision["snapshot_hash"]}})
        return self._response(revision)

    def diff(self, material_id, revision_id=None, include_unchanged=False):
        with self.repository.transaction():
            material = self._lock_work(material_id)
            history = self._history(material_id)
            if revision_id:
                revision = next((row for row in history if row["id"] == revision_id), None)
                if revision is None:
                    raise DomainNotFound("graph_revision", revision_id)
            else:
                published = self._published(history)
                current_base = published['graph_version'] if published else 0
                candidates = [row for row in history if row['material_version_id'] == material.current_version_id
                              and row['base_graph_version'] == current_base
                              and row["status"] in {"draft", "pending_review", "failed"}]
                revision = candidates[-1] if candidates else None
            if revision is None:
                published = self._published(history)
                result = {"base_graph_version": published["graph_version"] if published else 0,
                          "candidate_revision_id": None, "added": [], "changed": [], "removed": [],
                          "conflicts": [], "affected_topic_ids": []}
                facts = self.lifecycle(material_id)
                result.update(status=facts['stage'], error=facts['error'],
                              material_version_id=facts['material_version_id'], next_action=facts['next_action'])
                if include_unchanged:
                    result["unchanged"] = []
                return result
            result = copy.deepcopy(revision["diff"])
            if not include_unchanged:
                result.pop("unchanged", None)
            result.update(base_graph_version=revision["base_graph_version"], candidate_revision_id=revision["id"],
                          material_version_id=revision["material_version_id"], status=revision["status"])
            run = self.runs.get(revision['run_id'])
            result['error'] = run.get('error')
            if revision['status'] == 'draft' and run['status'] in {'failed', 'cancelling', 'cancelled'}:
                result['status'] = 'failed'
                result['error'] = run.get('error') or {'code': 'MATERIAL_TASK_CANCELLED',
                    'message': '任务已取消，可重新提交任务', 'retryable': True,
                    'details': {'stage': 'preparing', 'retryable': True}}
            return result

    def publish(self, material_id, revision_id, payload, key, *, correction_id=None):
        def change():
            material = self._lock_work(material_id)
            history = self._history(material_id)
            revision = next((row for row in history if row["id"] == revision_id), None)
            if revision is None:
                raise DomainNotFound("graph_revision", revision_id)
            if revision['material_version_id'] != material.current_version_id and not correction_id:
                raise DomainConflict('MATERIAL_VERSION_STALE', '候选属于历史资料版本，请审核当前版本')
            if revision['diff'].get('correction_id'):
                if revision['diff']['correction_id'] != correction_id:
                    raise DomainConflict('CORRECTION_CONFIRMATION_REQUIRED', '纠错候选必须经纠错确认入口发布')
                correction = self.repository.get_record('corrections', correction_id)
                if correction['status'] != 'confirming' or not correction.get('confirmation'):
                    raise DomainConflict('CORRECTION_CONFIRMATION_REQUIRED', '纠错尚未确认')
            published = self._published(history)
            current = published["graph_version"] if published else 0
            if payload["expected_graph_version"] != current or revision["base_graph_version"] != current:
                raise DomainConflict("VERSION_CONFLICT", "图谱版本已变化，请重新生成候选快照")
            if correction_id and (not published or published['material_version_id'] != revision['material_version_id']):
                raise DomainConflict('VERSION_CONFLICT', '纠错来源不属于当前发布快照，请重新提交')
            known = {item["conflict_id"] for item in revision["diff"]["conflicts"]}
            supplied = {item["conflict_id"] for item in payload.get("resolutions", [])}
            if supplied - known:
                raise DomainConflict("INVALID_CONFLICT_RESOLUTION", "冲突不属于该候选快照")
            from knowpath_backend.learning.workers.graph import receipt_valid
            from knowpath_backend.learning.knowledge.schemas import PublishGraph
            validated = PublishGraph.model_validate(payload)
            if revision["status"] != "pending_review" or not receipt_valid(revision, revision.get("preparation")):
                raise DomainConflict("REVISION_NOT_READY", "候选图谱和索引尚未准备就绪，当前不能发布", {"candidate_revision_id": revision_id})
            if known - supplied:
                raise DomainConflict("GRAPH_CONFLICTS_PENDING", "请处理所有图谱冲突后再发布")
            decisions = {item.conflict_id: item.model_dump() for item in validated.resolutions}
            effective = copy.deepcopy(revision["snapshot"])
            nodes = {node["id"]: node for node in effective["nodes"]}
            for conflict in revision["diff"]["conflicts"]:
                decision = decisions[conflict["conflict_id"]]
                if decision["action"] == "keep_old":
                    nodes[conflict["topic_id"]] = copy.deepcopy(conflict["before"])
                elif decision["action"] == "keep_both":
                    node = nodes[conflict["topic_id"]]
                    node["alternatives"] = [copy.deepcopy(conflict["before"]), copy.deepcopy(conflict["after"])]
                    refs = {ref["chunk_id"]: ref for side in node["alternatives"] for ref in side["source_refs"]}
                    node["source_refs"] = list(refs.values())
                    node["status"], node["automatic_questions"] = "conflicted", False
                    node["content_hash"] = digest([side["content_hash"] for side in node["alternatives"]])
            effective["nodes"] = list(nodes.values())
            kept_old = {conflict["topic_id"] for conflict in revision["diff"]["conflicts"]
                        if decisions[conflict["conflict_id"]]["action"] == "keep_old"}
            if kept_old and published:
                previous_edges = {edge["id"]: edge for edge in published["publication"]["snapshot"]["relations"]}
                edges = {edge["id"]: edge for edge in effective["relations"]}
                for identifier, edge in list(edges.items()):
                    if kept_old.intersection((edge["from_id"], edge["to_id"])):
                        if identifier in previous_edges:
                            edges[identifier] = copy.deepcopy(previous_edges[identifier])
                        elif edge.get("status") not in {"rejected", "superseded", "inactive"}:
                            # Keeping an old assertion does not implicitly accept
                            # new relationships asserted only by rejected content.
                            edge.update(status="pending", confirmed=False, review_required=True)
                for identifier, edge in previous_edges.items():
                    if kept_old.intersection((edge["from_id"], edge["to_id"])):
                        edges.setdefault(identifier, copy.deepcopy(edge))
                effective["relations"] = list(edges.values())
            sources = {source["id"]: source for source in effective["sources"]}
            if published:
                sources.update({source["id"]: source for source in published["publication"]["snapshot"]["sources"]})
            used = {ref["chunk_id"] for node in effective["nodes"] for ref in node["source_refs"]}
            used.update(ref["chunk_id"] for edge in effective["relations"] for ref in edge.get("source_refs", []))
            if not used <= sources.keys():
                raise DomainConflict("INVALID_RELATION_SOURCE", "正式快照不能丢失节点或关系引用的来源")
            effective["sources"] = [sources[identifier] for identifier in sorted(used)]
            confirm_grounded_relations(effective)
            timestamp = now()
            revision.update(status="published", graph_version=current + 1,
                publication={"snapshot": effective, "snapshot_hash": digest(effective),
                             "resolutions": list(decisions.values()), "published_at": timestamp})
            if correction_id:
                revision['publication']['correction'] = {
                    'correction_id': correction_id, 'kind': correction['kind'], 'target_id': correction['target_id'],
                    'action': correction['action'], 'reason': correction['reason'], 'source_ref': correction['source_ref'],
                    'confirmation': copy.deepcopy(correction['confirmation'])}
            if published:
                published["status"] = "superseded"
                self.repository.put_record("graph_revisions", published)
            self._invalidate(history, base=current, except_id=revision_id)
            self.repository.put_record("graph_revisions", revision)
            return {"graph_version": current + 1, "material_version_id": revision["material_version_id"],
                    "candidate_revision_id": revision_id, "published_at": timestamp}
        return self.commands._execute("graph.publish", material_id, {"revision_id": revision_id, **payload}, key, change)

    def delete_history(self, material_id):
        """Called inside material deletion; no queued work may outlive its sources."""
        with self.repository.transaction():
            self._material(material_id)
            history = self._history(material_id)
            for version in self.materials.list_versions(material_id):
                for event in self.repository.records("outbox", event_type="material.parse", aggregate_id=version.id):
                    self.runs.request_cancel(event["payload"]["run_id"])
                    self.runs.acknowledge_cancel(event["payload"]["run_id"])
            for revision in history:
                self.runs.request_cancel(revision["run_id"])
                self.runs.acknowledge_cancel(revision["run_id"])
            self.repository.delete_history(material_id, {row["id"] for row in history})
