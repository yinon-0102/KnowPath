"""Learning-space metadata and explicit profiles, independent of HTTP transport."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, OperationalError

from .errors import DomainConflict, DomainNotFound
from .graph_relations import structural_topics, apply_prerequisites


def now():
    return datetime.now(timezone.utc).isoformat()


def topics_for_version(material_id, version):
    return structural_topics(material_id, version)


class SpaceService:
    def __init__(self, repository, materials):
        self.repository = repository
        self.materials = materials
        self.graphs = None

    def _execute(self, operation, space_id, payload, key, change):
        fingerprint = sha256(json.dumps([operation, space_id, payload], sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        for attempt in range(3):
            try:
                with self.repository.transaction():
                    if key:
                        replay = self.repository.replay(key, fingerprint)
                        if replay is not None:
                            return replay
                    result = change()
                    if key:
                        self.repository.remember(key, fingerprint, space_id or result["id"], result)
                    return result
            except IntegrityError:
                if not key or attempt == 2 or self.repository.replay(key, fingerprint) is None:
                    raise
            except DomainConflict as exc:
                # A concurrent same-key command may have committed a terminal
                # state since this transaction first checked its replay record.
                if key:
                    replay = self.repository.replay(key, fingerprint)
                    if replay is not None:
                        return replay
                raise
            except OperationalError as exc:
                if attempt == 2 or not exc.orig.args or exc.orig.args[0] not in {1205, 1213}:
                    raise
        raise AssertionError("unreachable")

    def create(self, payload, key=None, *, require_published=False):
        def change():
            material_ids = list(dict.fromkeys(payload.get("material_ids") or []))
            if not material_ids:
                raise DomainConflict("MATERIAL_REQUIRED", "至少需要一份资料")
            bindings = []
            for material_id in sorted(material_ids):
                material = self.materials.get_material(material_id)
                if material is None:
                    raise DomainNotFound("material", material_id)
                if material.status == "archived":
                    raise DomainConflict("MATERIAL_ARCHIVED", "归档资料不能绑定到新空间", {"material_id": material_id})
                published = self.graphs._published(self.graphs._history(material_id)) if self.graphs else None
                if published:
                    bindings.append({"material_id": material_id, "material_version_id": published["material_version_id"],
                                     "graph_version": published["graph_version"], "graph_revision_id": published["id"]})
                    continue
                if require_published:
                    raise DomainConflict("MATERIAL_NOT_READY", "资料还没有已发布的知识图谱快照", {"material_id": material_id})
                version = self.materials.get_version(material.current_version_id)
                if version is None or version.status != "ready":
                    raise DomainConflict("MATERIAL_NOT_READY", "资料还没有可用版本")
                bindings.append({"material_id": material_id, "material_version_id": version.id, "graph_version": 1})
            timestamp = now()
            space = {"id": str(uuid4()), "name": payload.get("name", "未命名学习空间"), "status": "draft",
                     "goal": payload.get("goal"), "target_date": payload.get("target_date"),
                     "weekly_minutes": payload.get("weekly_minutes"), "bindings": bindings,
                     "topic_ids": [], "excluded_topic_ids": [], "scope_version": 0,
                     "space_version": 1, "profile_version": 1, "state_version": 0,
                     "profile": {field: {"value": payload.get(field), "source": "explicit", "updated_at": timestamp}
                                 for field in ("goal", "weekly_minutes", "target_date")},
                     "created_at": timestamp, "updated_at": timestamp}
            self.repository.put(space)
            return {**space, "state_version": 0, "state": {}, "active_session_id": None}
        return self._execute("space.create", None, payload, key, change)

    def get(self, space_id):
        return self.repository.get(space_id)

    def list(self):
        return self.repository.list()

    @staticmethod
    def check_version(space, payload, field="space_version"):
        if payload.get("expected_version") != space[field]:
            raise DomainConflict("VERSION_CONFLICT", "资源版本已变化，请刷新后重试")

    def update(self, space_id, payload, key=None):
        def change():
            space = self.repository.get(space_id)
            self.check_version(space, payload)
            for field in ("name", "status"):
                if field in payload:
                    space[field] = payload[field]
            space["space_version"] += 1
            space["updated_at"] = now()
            self.repository.put(space)
            return space
        return self._execute("space.update", space_id, payload, key, change)

    def bound_topics(self, space):
        topics = {}
        for binding in space["bindings"]:
            version = self.materials.get_version(binding["material_version_id"])
            if version is None or version.material_id != binding["material_id"]:
                raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "空间绑定的资料版本不可用")
            if binding.get("graph_revision_id"):
                if self.graphs is None:
                    raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "图谱快照不可用")
                revision = self.graphs.repository.get_record("graph_revisions", binding["graph_revision_id"], lock=False)
                if (revision["material_id"] != binding["material_id"] or revision["material_version_id"] != version.id
                        or revision["graph_version"] != binding["graph_version"] or revision["status"] not in {"published", "superseded"}):
                    raise DomainConflict("BOUND_VERSION_UNAVAILABLE", "图谱快照绑定不匹配")
                snapshot = copy.deepcopy(revision["publication"]["snapshot"])
                apply_prerequisites(snapshot)
                bound = snapshot["nodes"]
                for node in bound:
                    node["graph_version"] = binding["graph_version"]
            else:
                bound = topics_for_version(binding["material_id"], version)
            for topic in bound:
                if topic.get("status") == "rejected":
                    continue
                topic["canonical_topic_id"] = topic["id"]
                topic["learning_revision_id"] = binding.get("topic_revision_ids", {}).get(topic["id"])
                topics[topic["id"]] = topic
                slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in topic["name"]).strip("_")
                if slug and len(space["bindings"]) == 1:
                    alias = f"topic_{slug}"
                    if alias not in topics:
                        alias_topic = copy.deepcopy(topic)
                        alias_topic["id"] = alias
                        alias_topic["learning_revision_id"] = binding.get("topic_revision_ids", {}).get(alias)
                        topics[alias] = alias_topic
        aliases = {topic["canonical_topic_id"]: identifier for identifier, topic in topics.items()
                   if identifier != topic["canonical_topic_id"]}
        for identifier, topic in topics.items():
            if identifier != topic["canonical_topic_id"]:
                for field in ("prerequisites", "recommended_prerequisites"):
                    topic[field] = [aliases.get(parent, parent) for parent in topic.get(field, [])]
        return list(topics.values())

    def set_scope(self, space_id, payload, key=None):
        def change():
            space = self.repository.get(space_id)
            self.check_version(space, payload)
            selected = list(dict.fromkeys(payload.get("topic_ids") or []))
            excluded = list(dict.fromkeys(payload.get("excluded_topic_ids") or []))
            topics = {topic["id"]: topic for topic in self.bound_topics(space)}
            available = set(topics)
            if not selected or not set(selected + excluded) <= available or set(selected) & set(excluded):
                raise DomainConflict("SCOPE_CONFLICT", "学习范围必须非空，来自绑定快照且不与排除项重叠")
            canonical = lambda identifier: topics[identifier]["canonical_topic_id"]
            excluded_canonical = {canonical(identifier) for identifier in excluded}
            if {canonical(identifier) for identifier in selected} & excluded_canonical:
                raise DomainConflict("SCOPE_CONFLICT", "学习范围不能通过别名包含排除主题")
            chosen = {canonical(identifier) for identifier in selected}
            prerequisites, recommendations = [], set()
            pending = list(selected)
            while pending:
                identifier = pending.pop(0)
                topic = topics[identifier]
                recommendations.update(parent for parent in topic.get("recommended_prerequisites", [])
                                       if parent in topics and canonical(parent) not in excluded_canonical)
                for parent in topic.get("prerequisites", []):
                    if parent not in topics or canonical(parent) in excluded_canonical or canonical(parent) in chosen:
                        continue
                    if payload.get("include_prerequisites", True):
                        selected.append(parent)
                        prerequisites.append(parent)
                        chosen.add(canonical(parent))
                        pending.append(parent)
                    else:
                        recommendations.add(parent)
            recommended = sorted(identifier for identifier in recommendations if canonical(identifier) not in chosen)
            space["topic_ids"], space["excluded_topic_ids"] = selected, excluded
            space["space_version"] += 1
            space["scope_version"] += 1
            if self.graphs is not None:
                for plan in self.graphs.repository.records("plans", space_id=space_id):
                    if plan["status"] in {"active", "ready", "draft"}:
                        plan["status"] = "needs_replan"
                        self.graphs.repository.put_record("plans", plan)
            space["updated_at"] = now()
            self.repository.put(space)
            return {"scope_version": space["scope_version"], "space_version": space["space_version"],
                    "topic_ids": selected, "prerequisite_topic_ids": prerequisites, "recommended_topic_ids": recommended}
        return self._execute("space.scope", space_id, payload, key, change)

    def update_profile(self, space_id, payload, key=None):
        def change():
            space = self.repository.get(space_id)
            self.check_version(space, payload, "profile_version")
            timestamp = now()
            for field in ("goal", "weekly_minutes", "target_date", "preferences"):
                if field not in payload:
                    continue
                value = payload[field]
                if field == "preferences":
                    existing = copy.deepcopy(space["profile"].get(field, {}).get("value") or {})
                    if value is None:
                        space["profile"].pop(field, None)
                        continue
                    for preference, enabled in value.items():
                        if enabled is None:
                            existing.pop(preference, None)
                        else:
                            existing[preference] = enabled
                    value = existing
                space["profile"][field] = {"value": value, "source": "explicit", "updated_at": timestamp}
                if field != "preferences":
                    space[field] = value
            if self.graphs is not None:
                from .profile_candidates import confirm_candidates
                confirm_candidates(self.graphs.repository, space_id, payload.get("preferences"), timestamp)
            space["profile_version"] += 1
            space["updated_at"] = timestamp
            self.repository.put(space)
            return {"profile": copy.deepcopy(space["profile"]), "profile_version": space["profile_version"]}
        return self._execute("space.profile", space_id, payload, key, change)

    def delete(self, space_id):
        with self.repository.transaction():
            self.repository.get(space_id)
            self.repository.delete(space_id)
