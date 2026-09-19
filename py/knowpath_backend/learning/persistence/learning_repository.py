"""Transactional assessment, answer and evidence stores sharing the space unit of work."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, exists

from knowpath_backend.learning.persistence.db import AssessmentRow, AttemptRow, EvidenceRow, LearnerStateRow, StateResetRow, IdempotencyRow, StudyPlanRow, StudyTaskRow, SessionRow, SessionEventRow, ConversationRow, LearningMessageRow, GraphRevisionRow, OutboxEventRow, KnowledgeCorrectionRow
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.persistence.space_repository import InMemorySpaceRepository, SqlAlchemySpaceRepository


TABLES = {"assessments": AssessmentRow, "attempts": AttemptRow, "evidence": EvidenceRow,
          "states": LearnerStateRow, "resets": StateResetRow,
          "plans": StudyPlanRow, "tasks": StudyTaskRow, "sessions": SessionRow, "session_events": SessionEventRow, "conversations": ConversationRow, "messages": LearningMessageRow, "graph_revisions": GraphRevisionRow, "outbox": OutboxEventRow, "corrections": KnowledgeCorrectionRow}
DATE_FIELDS = {"lease_until", "available_at", "created_at", "last_assessed_at", "next_review_at", "defer_until", "started_at", "finished_at", "received_at"}


class InMemoryLearningRepository(InMemorySpaceRepository):
    def __init__(self, materials, runs):
        super().__init__(materials)
        self.runs = runs

    @contextmanager
    def transaction(self):
        with self.materials.transaction(), self.runs.repository.transaction():
            yield

    def get_record(self, table, identifier, *, lock=True):
        with self.materials._lock:
            row = self.materials.assessment_data[table].get(identifier)
            if row is None:
                raise DomainNotFound(table, identifier)
            return copy.deepcopy(row)

    def put_record(self, table, value):
        with self.materials._lock:
            self.materials.assessment_data[table][value["id"]] = copy.deepcopy(value)

    def exists(self, table, **filters):
        with self.materials._lock:
            return any(all(row.get(key) == value for key, value in filters.items())
                       for row in self.materials.assessment_data[table].values())

    def records(self, table, *, lock=True, **filters):
        with self.materials._lock:
            rows = self.materials.assessment_data[table].values()
            return sorted([copy.deepcopy(row) for row in rows
                           if all(row.get(k) == v for k, v in filters.items())],
                          key=lambda row: (row.get("created_at", ""), row["id"]))


class SqlAlchemyLearningRepository(SqlAlchemySpaceRepository):
    def get_record(self, table, identifier, *, lock=True):
        cls = TABLES[table]
        with self.unit_of_work.session() as session:
            query = select(cls).where(cls.id == identifier)
            if lock and self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            row = session.scalar(query)
            if row is None:
                raise DomainNotFound(table, identifier)
            return self._decode(row)

    def exists(self, table, **filters):
        cls = TABLES[table]
        with self.unit_of_work.session() as session:
            query = select(exists().where(*[getattr(cls, key) == value for key, value in filters.items()]))
            return bool(session.scalar(query))

    def records(self, table, *, lock=True, **filters):
        cls = TABLES[table]
        with self.unit_of_work.session() as session:
            query = select(cls).filter_by(**filters).order_by(cls.id)
            if lock and self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            rows = [self._decode(row) for row in session.scalars(query)]
            return sorted(rows, key=lambda row: (row.get("created_at", ""), row["id"]))

    @staticmethod
    def _decode(row):
        value = {}
        for column in row.__table__.columns:
            item = copy.deepcopy(getattr(row, column.name))
            if column.name in DATE_FIELDS and item is not None:
                item = item.replace(tzinfo=timezone.utc).isoformat()
            if column.name == "context":
                value.update(item or {})
            else:
                value[column.name] = item
        return value

    def put_record(self, table, value):
        cls = TABLES[table]
        columns = {c.name for c in cls.__table__.columns}
        data = {k: copy.deepcopy(v) for k, v in value.items() if k in columns}
        if "context" in columns:
            data["context"] = {k: copy.deepcopy(v) for k, v in value.items() if k not in columns}
        for key in DATE_FIELDS & data.keys():
            if data[key] is not None:
                data[key] = datetime.fromisoformat(data[key]).astimezone(timezone.utc).replace(tzinfo=None)
        with self.unit_of_work.session() as session:
            # Parent aggregate locks serialize updates; immutable attempts/evidence
            # have distinct IDs and are only appended by the command service.
            session.merge(cls(**data))

    def replay(self, key, fingerprint):
        with self.unit_of_work.session() as session:
            row = session.get(IdempotencyRow, key)
            if row is None:
                return None
            if row.request_fingerprint != fingerprint or row.resource_type != "assessment_command" or row.response is None:
                raise DomainConflict("IDEMPOTENCY_CONFLICT", "相同幂等键已用于不同请求")
            if row.response.get("resource_deleted"):
                raise DomainConflict("RESOURCE_DELETED", "资源已删除，原请求不可重放")
            return copy.deepcopy(row.response)

    def remember(self, key, fingerprint, resource_id, response):
        with self.unit_of_work.session() as session:
            session.add(IdempotencyRow(key=key, request_fingerprint=fingerprint,
                resource_type="assessment_command", resource_id=resource_id,
                response=copy.deepcopy(response), created_at=datetime.now(timezone.utc)))
