"""SQL run repository; row locks serialize state changes and event sequence allocation."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from .db import RunEventRow, RunRow
from .errors import DomainNotFound
from .runs import ACTIVE_STATUSES, Run
from .unit_of_work import SqlAlchemyUnitOfWork


def _iso(value: datetime | None) -> str | None:
    # MySQL and SQLite may return UTC database timestamps without tzinfo.
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None) if value else None


class SqlAlchemyRunRepository:
    def __init__(self, engine, *, unit_of_work=None):
        self.unit_of_work = unit_of_work or SqlAlchemyUnitOfWork(engine)

    def transaction(self):
        return self.unit_of_work.transaction()

    @staticmethod
    def _read(session, row):
        events = session.scalars(select(RunEventRow).where(RunEventRow.run_id == row.id)
                                 .order_by(RunEventRow.sequence)).all()
        return {"id": row.id, "kind": row.kind, "status": row.status,
                "progress": row.progress, "result_ref": copy.deepcopy(row.result_ref),
                "error": copy.deepcopy(row.error), "created_at": _iso(row.created_at),
                "finished_at": _iso(row.finished_at),
                "events": [{"id": str(e.sequence), "event": e.event,
                            "data": copy.deepcopy(e.data), "created_at": _iso(e.created_at)} for e in events]}

    @staticmethod
    def _write_fields(row, run):
        for field in ("kind", "status", "progress", "result_ref", "error"):
            setattr(row, field, copy.deepcopy(run[field]))
        row.created_at = _date(run["created_at"])
        row.finished_at = _date(run["finished_at"])

    @staticmethod
    def _event_row(run_id, event):
        return RunEventRow(run_id=run_id, sequence=int(event["id"]), event=event["event"],
                           data=copy.deepcopy(event["data"]), created_at=_date(event["created_at"]))

    def create(self, run: Run) -> None:
        with self.unit_of_work.transaction(), self.unit_of_work.session() as session:
            row = RunRow(id=run["id"])
            self._write_fields(row, run)
            session.add(row)
            session.flush()
            session.add_all(self._event_row(run["id"], e) for e in run["events"])

    def get(self, run_id: str) -> Run:
        with self.unit_of_work.session() as session:
            query = select(RunRow).where(RunRow.id == run_id)
            if self.unit_of_work.active:
                query = query.with_for_update().execution_options(populate_existing=True)
            row = session.scalar(query)
            if row is None:
                raise DomainNotFound("run", run_id)
            return self._read(session, row)

    def mutate(self, run_id: str, change: Callable[[Run], None]) -> Run:
        with self.unit_of_work.transaction(), self.unit_of_work.session() as session:
            row = session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            if row is None:
                raise DomainNotFound("run", run_id)
            run = self._read(session, row)
            count = len(run["events"])
            change(run)
            self._write_fields(row, run)
            for event in run["events"][:count]:
                if event["data"] is None:
                    stored = session.get(RunEventRow, (run_id, int(event["id"])))
                    stored.data = None
            session.add_all(self._event_row(run_id, e) for e in run["events"][count:])
            return copy.deepcopy(run)

    def active_ids(self) -> list[str]:
        with self.unit_of_work.session() as session:
            return list(session.scalars(select(RunRow.id).where(RunRow.status.in_(ACTIVE_STATUSES))))
