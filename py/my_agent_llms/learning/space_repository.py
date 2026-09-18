"""Transactional repositories for space metadata, profiles and command replay."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from sqlalchemy import select

from .db import IdempotencyRow, LearningSpaceRow
from .errors import DomainConflict, DomainNotFound


FIELDS = ("id", "name", "status", "goal", "target_date", "weekly_minutes", "bindings",
          "topic_ids", "excluded_topic_ids", "space_version", "scope_version", "profile_version", "profile", "state_version")


def _conflict():
    return DomainConflict("IDEMPOTENCY_CONFLICT", "相同幂等键已用于不同请求")


class InMemorySpaceRepository:
    def __init__(self, materials):
        self.materials = materials

    def transaction(self):
        return self.materials.transaction()

    def get(self, space_id):
        with self.materials._lock:
            space = self.materials.learning_spaces.get(space_id)
            if space is None:
                raise DomainNotFound("learning_space", space_id)
            return copy.deepcopy(space)

    def list(self):
        with self.materials._lock:
            return copy.deepcopy(list(self.materials.learning_spaces.values()))

    def put(self, space):
        with self.materials._lock:
            self.materials.learning_spaces[space["id"]] = copy.deepcopy(space)

    def delete(self, space_id):
        with self.materials._lock:
            self.materials.learning_spaces.pop(space_id, None)

    def replay(self, key, fingerprint):
        with self.materials._lock:
            record = self.materials.idempotency.get(key)
            if record is None:
                return None
            response = self.materials.idempotency_responses.get(key)
            if record[0] != fingerprint or response is None:
                raise _conflict()
            return copy.deepcopy(response)

    def remember(self, key, fingerprint, space_id, response):
        self.materials.idempotency[key] = (fingerprint, space_id, None)
        self.materials.idempotency_responses[key] = copy.deepcopy(response)


class SqlAlchemySpaceRepository:
    def __init__(self, unit_of_work):
        self.unit_of_work = unit_of_work

    def transaction(self):
        return self.unit_of_work.transaction()

    def get(self, space_id):
        with self.unit_of_work.session() as session:
            query = select(LearningSpaceRow).where(LearningSpaceRow.id == space_id)
            if self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            row = session.scalar(query)
            if row is None:
                raise DomainNotFound("learning_space", space_id)
            return self._read(row)

    def list(self):
        with self.unit_of_work.session() as session:
            query = select(LearningSpaceRow).order_by(LearningSpaceRow.created_at, LearningSpaceRow.id)
            if self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            return [self._read(row) for row in session.scalars(query)]

    @staticmethod
    def _read(row):
        space = {key: copy.deepcopy(getattr(row, key)) for key in FIELDS}
        for key in ("created_at", "updated_at"):
            space[key] = getattr(row, key).replace(tzinfo=timezone.utc).isoformat()
        return space

    def put(self, space):
        with self.unit_of_work.session() as session:
            values = {key: copy.deepcopy(space[key]) for key in FIELDS}
            for key in ("created_at", "updated_at"):
                values[key] = datetime.fromisoformat(space[key]).astimezone(timezone.utc).replace(tzinfo=None)
            session.merge(LearningSpaceRow(**values))

    def delete(self, space_id):
        with self.unit_of_work.session() as session:
            row = session.get(LearningSpaceRow, space_id)
            if row is not None:
                session.delete(row)

    def replay(self, key, fingerprint):
        with self.unit_of_work.session() as session:
            row = session.get(IdempotencyRow, key)
            if row is None:
                return None
            if row.request_fingerprint != fingerprint or row.resource_type != "learning_space" or row.response is None:
                raise _conflict()
            return copy.deepcopy(row.response)

    def remember(self, key, fingerprint, space_id, response):
        with self.unit_of_work.session() as session:
            session.add(IdempotencyRow(key=key, request_fingerprint=fingerprint,
                                       resource_type="learning_space", resource_id=space_id,
                                       response=copy.deepcopy(response), created_at=datetime.now(timezone.utc)))
