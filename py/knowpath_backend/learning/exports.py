"""Immutable, allowlisted learning exports sharing the domain transaction boundary."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import delete

from knowpath_backend.learning.persistence.db import ExportRow
from .errors import DomainConflict, DomainNotFound
from .spaces import SpaceService


SPACE_FIELDS = ("id", "name", "status", "goal", "target_date", "weekly_minutes", "topic_ids",
                "excluded_topic_ids", "space_version", "scope_version", "profile_version",
                "state_version", "created_at", "updated_at")
STATE_FIELDS = ("id", "space_id", "topic_id", "topic_revision_id", "mastery_score", "score_validity",
                "status", "evidence_ids", "error_tags", "state_version", "policy_version",
                "last_assessed_at", "next_review_at", "epoch", "confidence", "evidence_count", "independent_evidence_count")
EVIDENCE_FIELDS = ("id", "space_id", "topic_id", "assessment_id", "attempt_id", "question_id",
                   "submission_id", "topic_revision_id", "family_id", "rubric_version", "is_application",
                   "assisted", "eligible", "epoch", "submission_sequence", "kind", "result", "score",
                   "error_tags", "created_at", "review_id", "reviewed_at", "replaces_evidence_id",
                   "observation_id", "revoked_by_review_id", "revoked_at")
PLAN_FIELDS = ("id", "plan_id", "space_id", "version", "status", "scope_version", "run_id", "created_at")
TASK_FIELDS = ("id", "topic_ids", "kind", "status", "estimated_minutes", "reason", "note", "defer_until")
SESSION_FIELDS = ("id", "space_id", "plan_id", "task_id", "status", "started_at", "finished_at")
EVENT_FIELDS = ("id", "event_id", "type", "topic_id", "question_id", "received_at")
CONFIG_FIELDS = ("session_count", "minutes_per_session", "include_review", "rebuild_mode",
                 "base_plan_id", "expected_plan_version")
BINDING_FIELDS = ("material_id", "material_version_id", "graph_version")


def _pick(value, fields):
    """Only scalar DTO fields and scalar lists cross the export boundary."""
    result = {}
    for key in fields:
        item = value.get(key)
        if key not in value:
            continue
        if item is None or isinstance(item, (str, bool, int, float)):
            result[key] = item
        elif isinstance(item, list):
            result[key] = [x for x in item if x is None or isinstance(x, (str, bool, int, float))]
    return result


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ExportService:
    """Create synchronously; persist only the frozen public DTO, never live objects."""

    def __init__(self, repository, spaces, assessments, plans, runs, *, clock=None):
        self.repository, self.spaces = repository, spaces
        self.assessments, self.plans, self.runs = assessments, plans, runs
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.uow = getattr(repository, "unit_of_work", None)
        # Reuse the command replay and retry policy with the learning UoW, which
        # also snapshots the run repository in the memory adapter.
        self.commands = SpaceService(repository, spaces.materials)

    def create(self, space_id, payload=None, idempotency_key=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict) or set(payload) - {"format"} or payload.get("format", "json") != "json":
            raise DomainConflict("INVALID_REQUEST", "导出仅支持 format=json")
        request = {"format": "json"}

        def change():
            space = self.spaces.get(space_id)
            created_at = _utc(self.clock())
            export_id = str(uuid4())
            snapshot = self._snapshot(space, created_at)
            run = self.runs.create("export", {"type": "export", "id": export_id}, status="succeeded")
            record = {"id": export_id, "space_id": space_id, "run_id": run["id"], "status": "ready",
                      "payload": snapshot, "created_at": created_at,
                      "expires_at": created_at + timedelta(hours=24)}
            self._save(record)
            return {"run_id": run["id"], "export_id": export_id, "status": "ready"}

        return self.commands._execute("export.create", space_id, request, idempotency_key, change)

    def _save(self, record):
        if self.uow is not None:
            data = copy.deepcopy(record)
            for field in ("created_at", "expires_at"):
                data[field] = data[field].replace(tzinfo=None)
            with self.uow.session() as session:
                session.add(ExportRow(**data))
        else:
            with self.repository.materials._lock:
                self.repository.materials.export_data[record["id"]] = copy.deepcopy(record)

    def delete_for_space(self, space_id):
        """Called inside the space deletion transaction after locking its owner."""
        if self.uow is not None:
            with self.uow.session() as session:
                session.execute(delete(ExportRow).where(ExportRow.space_id == space_id))
        else:
            with self.repository.materials._lock:
                records = self.repository.materials.export_data
                for identifier in [key for key, value in records.items() if value["space_id"] == space_id]:
                    del records[identifier]

    def _load(self, export_id):
        if self.uow is not None:
            with self.uow.session() as session:
                row = session.get(ExportRow, export_id)
                record = None if row is None else {"payload": copy.deepcopy(row.payload), "expires_at": row.expires_at}
        else:
            with self.repository.materials._lock:
                record = copy.deepcopy(self.repository.materials.export_data.get(export_id))
        if record is None:
            raise DomainNotFound("export", export_id)
        if _utc(self.clock()) >= _utc(record["expires_at"]):
            raise DomainConflict("EXPORT_EXPIRED", "导出已过期，请重新创建导出", {"export_id": export_id})
        return record

    def payload(self, export_id):
        return self._load(export_id)["payload"]

    def archive(self, export_id):
        """The identifier is a database key; neither it nor material names are paths."""
        content = json.dumps(self.payload(export_id), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("learning-space.json", content)
        return output.getvalue()

    def _profile(self, profile):
        result = {}
        for field in ("goal", "weekly_minutes", "target_date", "preferences"):
            entry = profile.get(field)
            if not isinstance(entry, dict):
                continue
            public = _pick(entry, ("source", "updated_at"))
            if field == "preferences":
                value = entry.get("value")
                public["value"] = _pick(value, ("example_first", "concise_explanations")) if isinstance(value, dict) else None
            else:
                public.update(_pick(entry, ("value",)))
            result[field] = public
        return result

    def _snapshot(self, space, created_at):
        allowed_materials = {binding["material_id"] for binding in space["bindings"]}
        versions = {}
        references = {}

        def material_version(material_id, version_id):
            if material_id not in allowed_materials:
                return None
            if version_id not in versions:
                version = self.spaces.materials.get_version(version_id)
                if version is None or version.material_id != material_id:
                    return None
                versions[version_id] = version
            version = versions[version_id]
            return version if version.material_id == material_id else None

        def source_refs(items):
            result = []
            for ref in items or []:
                if not isinstance(ref, dict):
                    continue
                version = material_version(ref.get("material_id"), ref.get("material_version_id"))
                if version is None:
                    continue
                chunk = next((c for c in version.chunks if c.id == ref.get("chunk_id")), None)
                if chunk is None:
                    continue
                public = {"material_id": version.material_id, "material_version_id": version.id,
                          "chunk_id": chunk.id, "page": chunk.page, "line_start": chunk.line_start, "line_end": chunk.line_end}
                identity = (version.material_id, version.id, chunk.id)
                references[identity] = public
                if public not in result:
                    result.append(public)
            return result

        public_space = _pick(space, SPACE_FIELDS)
        public_space["profile"] = self._profile(space.get("profile", {}))
        public_space["bindings"] = [_pick(binding, BINDING_FIELDS) for binding in space["bindings"]]
        for binding in space["bindings"]:
            if material_version(binding["material_id"], binding["material_version_id"]) is None:
                raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "空间绑定的资料版本不可用")
        for topic in self.spaces.bound_topics(space):
            source_refs(topic.get("source_refs"))
        state = self.assessments.state(space["id"])
        public_space["state"] = {row["topic_id"]: _pick(row, STATE_FIELDS) for row in state["items"]}
        public_space["state_version"] = state["state_version"]
        evidence = []
        for row in self.assessments.evidence(space["id"]):
            evidence.append({**_pick(row, EVIDENCE_FIELDS), "source_refs": source_refs(row.get("source_refs"))})
        plans = []
        for row in self.repository.records("plans", space_id=space["id"]):
            plan = self.plans.get_plan(row["id"], lock=True)
            public = _pick(plan, PLAN_FIELDS)
            public["config"] = _pick(plan.get("config", {}), CONFIG_FIELDS)
            public["tasks"] = [_pick(task, TASK_FIELDS) for task in plan.get("tasks", [])]
            plans.append(public)
        sessions = []
        for row in self.repository.records("sessions", space_id=space["id"]):
            public = _pick(row, SESSION_FIELDS)
            # SQL repository flattens context; the memory adapter retains it.
            context = row.get("context", row)
            public["context"] = _pick(context, ("task_id", "topic_ids", "kind", "estimated_minutes", "reason"))
            public["context"]["topics"] = [_pick(t, ("id", "name")) for t in context.get("topics", [])]
            public["context"]["source_refs"] = source_refs(context.get("source_refs"))
            if self.uow is None:
                events = [copy.deepcopy(event) for event in self.repository.materials.assessment_data["session_events"].values()
                          if event.get("session_id") == row["id"]]
            else:
                events = self.repository.records("session_events", session_id=row["id"])
            public["events"] = []
            for event in events:
                # Event response is the validated public acknowledgement; never
                # export arbitrary event payloads, fingerprints or provider state.
                response = event.get("payload", event).get("response", {})
                public["events"].append(_pick(response, EVENT_FIELDS))
            public["events"].sort(key=lambda event: (event.get("received_at", ""), event.get("id", "")))
            sessions.append(public)
        manifest = {field: public_space[field] for field in ("space_version", "scope_version", "profile_version", "state_version")}
        manifest["materials"] = [{"material_id": version.material_id, "material_version_id": version.id,
                                  "content_hash": version.content_hash, "status": version.status,
                                  "created_at": _utc(version.created_at).isoformat()}
                                 for version in sorted(versions.values(), key=lambda v: v.id)]
        manifest["plans"] = [_pick(plan, ("id", "version", "scope_version")) for plan in plans]
        manifest["mastery_policy_versions"] = sorted({row["policy_version"] for row in public_space["state"].values() if row.get("policy_version")})
        return {"schema_version": "learning-export-v1", "created_at": created_at.isoformat(),
                "space": public_space, "evidence": evidence, "plans": plans, "sessions": sessions,
                "versions": manifest, "source_refs": [references[key] for key in sorted(references)]}
