"""Learning-space metadata and explicit profiles, independent of HTTP transport."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, OperationalError

from .errors import DomainConflict, DomainNotFound


def now():
    return datetime.now(timezone.utc).isoformat()


def topics_for_version(material_id, version):
    topics = {}
    for index, chunk in enumerate(version.chunks):
        name = chunk.section_path[-1] if chunk.section_path else chunk.text.split(".", 1)[0][:80]
        topic_id = "topic_" + sha256(f"{material_id}:{name}".encode()).hexdigest()[:12]
        topic = topics.setdefault(topic_id, {
            "id": topic_id, "name": name or f"知识点 {index + 1}", "kind": "concept", "parent_id": None,
            "level": len(chunk.section_path) or 1, "confidence": 1.0, "source_refs": [],
            "prerequisites": [], "status": "active", "graph_version": 1,
        })
        topic["source_refs"].append({"material_id": material_id, "material_version_id": version.id,
                                     "chunk_id": chunk.id, "page": chunk.page,
                                     "line_start": chunk.line_start, "line_end": chunk.line_end})
    return list(topics.values())


class SpaceService:
    def __init__(self, repository, materials):
        self.repository = repository
        self.materials = materials

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

    def create(self, payload, key=None):
        def change():
            material_ids = list(dict.fromkeys(payload.get("material_ids") or []))
            if not material_ids:
                raise DomainConflict("MATERIAL_REQUIRED", "至少需要一份资料")
            bindings = []
            for material_id in sorted(material_ids):
                material = self.materials.get_material(material_id)
                if material is None:
                    raise DomainNotFound("material", material_id)
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
            for topic in topics_for_version(binding["material_id"], version):
                topics[topic["id"]] = topic
                slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in topic["name"]).strip("_")
                if slug and len(space["bindings"]) == 1:
                    alias = f"topic_{slug}"
                    if alias not in topics:
                        alias_topic = copy.deepcopy(topic)
                        alias_topic["id"] = alias
                        topics[alias] = alias_topic
        return list(topics.values())

    def set_scope(self, space_id, payload, key=None):
        def change():
            space = self.repository.get(space_id)
            self.check_version(space, payload)
            selected = list(dict.fromkeys(payload.get("topic_ids") or []))
            excluded = list(dict.fromkeys(payload.get("excluded_topic_ids") or []))
            available = {topic["id"] for topic in self.bound_topics(space)}
            if not selected or not set(selected + excluded) <= available or set(selected) & set(excluded):
                raise DomainConflict("SCOPE_CONFLICT", "学习范围必须非空，来自绑定快照且不与排除项重叠")
            space["topic_ids"], space["excluded_topic_ids"] = selected, excluded
            space["space_version"] += 1
            space["scope_version"] += 1
            space["updated_at"] = now()
            self.repository.put(space)
            return {"scope_version": space["scope_version"], "space_version": space["space_version"],
                    "topic_ids": selected, "prerequisite_topic_ids": [], "recommended_topic_ids": []}
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
            space["profile_version"] += 1
            space["updated_at"] = timestamp
            self.repository.put(space)
            return {"profile": copy.deepcopy(space["profile"]), "profile_version": space["profile_version"]}
        return self._execute("space.profile", space_id, payload, key, change)

    def delete(self, space_id):
        with self.repository.transaction():
            self.repository.get(space_id)
            self.repository.delete(space_id)
