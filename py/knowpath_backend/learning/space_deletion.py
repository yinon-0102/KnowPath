"""Confirmed, atomic removal of a space's owned learning records."""
from sqlalchemy import delete, select, or_

from knowpath_backend.learning.persistence.db import (ExportRow, KnowledgeCorrectionRow, OutboxEventRow, IdempotencyRow,
                 RunRow, RunEventRow)
from .errors import DomainConflict
from .model_tasks import EVENTS as MODEL_EVENTS
from knowpath_backend.learning.persistence.learning_repository import TABLES
from .space_schemas import DeleteSpace
from .spaces import SpaceService


class SpaceDeletionService:
    def __init__(self, repository, spaces, runs):
        self.repository, self.spaces, self.runs = repository, spaces, runs
        self.uow = getattr(repository, "unit_of_work", None)
        self.commands = SpaceService(repository, spaces.materials)

    def delete(self, space_id, payload):
        payload = DeleteSpace.model_validate(payload).model_dump()

        def change():
            # Match finalize/review's assessment -> space order. The command
            # retries MySQL deadlocks caused by a concurrent assessment insert.
            assessments = self.repository.records("assessments", space_id=space_id)
            space = self.spaces.repository.get(space_id)
            if space["space_version"] != payload["expected_version"]:
                raise DomainConflict("VERSION_CONFLICT", "学习空间版本已变化")
            # The worker holds this same space lock before material/revision locks.
            # Candidate history belongs to the material, but deleted spaces cannot publish corrections.
            for correction in self.repository.records('corrections', space_id=space_id):
                if correction.get('candidate_revision_id') and correction['status'] != 'published':
                    self.spaces.materials.get_material(correction['material_id'])
                    self.repository.get_record('graph_revisions', correction['candidate_revision_id'])
                    for event in self.repository.records('outbox', aggregate_id=correction['candidate_revision_id']):
                        event.update(status='cancelled', lease_token=None, lease_until=None)
                        self.repository.put_record('outbox', event)
                    self.runs.request_cancel(correction['run_id'])
                    self.runs.acknowledge_cancel(correction['run_id'])
            assessments = self.repository.records("assessments", space_id=space_id)
            plans = self.repository.records("plans", space_id=space_id)
            sessions = self.repository.records("sessions", space_id=space_id)
            messages = self.repository.records("messages", space_id=space_id)
            ids = {space_id} | {r["id"] for r in assessments + plans + sessions + messages}
            run_ids = {r[k] for r in assessments + plans for k in ("run_id", "finalize_run_id") if r.get(k)}
            run_ids.update(m["run_id"] for m in messages)
            for assessment in assessments:
                run_ids.update(r["run_id"] for r in assessment.get("grade_reviews", []))
            self._remove_owned_outbox(space_id, ids, run_ids)
            if self.uow is None:
                self._memory(space_id, assessments, plans, sessions, ids, run_ids)
            else:
                self._sql(space_id, assessments, plans, sessions, ids, run_ids)
            self.spaces.repository.delete(space_id)
            run = self.runs.create("space_delete", {"type": "learning_space", "id": space_id}, status="succeeded")
            return {"run_id": run["id"], "space_id": space_id, "status": "succeeded"}

        return self.commands._execute("space.delete", space_id, payload, None, change)

    def _remove_owned_outbox(self, space_id, ids, run_ids):
        # Domain owners are already locked. Inspect without locking other spaces'
        # jobs, then lock/remove only this space's events before removing its Runs.
        # Include model orphans by payload and knowledge.updated by aggregate_id.
        owned = [row for row in self.repository.records("outbox", lock=False)
                 if row["aggregate_id"] in ids or (row["event_type"] in MODEL_EVENTS
                     and row["payload"].get("space_id") == space_id)]
        for candidate in owned:
            event = self.repository.get_record("outbox", candidate["id"])
            if event["payload"].get("run_id"):
                run_ids.add(event["payload"]["run_id"])
            if self.uow is None:
                self.repository.materials.assessment_data["outbox"].pop(event["id"], None)
            else:
                with self.uow.session() as session:
                    session.execute(delete(OutboxEventRow).where(OutboxEventRow.id == event["id"]))

    @staticmethod
    def _filters(space_id, assessments, plans, sessions):
        return [("corrections", "space_id", {space_id}),
                ("messages", "space_id", {space_id}),
                ("conversations", "space_id", {space_id}),
                ("session_events", "session_id", {r["id"] for r in sessions}),
                ("sessions", "space_id", {space_id}),
                ("tasks", "plan_id", {r["id"] for r in plans}),
                ("plans", "space_id", {space_id}),
                ("evidence", "space_id", {space_id}),
                ("states", "space_id", {space_id}),
                ("resets", "space_id", {space_id}),
                ("attempts", "assessment_id", {r["id"] for r in assessments}),
                ("assessments", "space_id", {space_id})]

    def _memory(self, space_id, assessments, plans, sessions, ids, run_ids):
        materials = self.repository.materials
        for identifier, row in list(materials.export_data.items()):
            if row["space_id"] == space_id:
                ids.add(identifier)
                run_ids.add(row["run_id"])
                del materials.export_data[identifier]
        for table, field, values in self._filters(space_id, assessments, plans, sessions):
            records = materials.assessment_data[table]
            for identifier, row in list(records.items()):
                if row.get(field) in values:
                    ids.add(identifier)
                    del records[identifier]
        run_repo = self.runs.repository
        for identifier, run in list(run_repo._runs.items()):
            reference = run.get("result_ref") or {}
            if identifier in run_ids or reference.get("id") in ids or reference.get("space_id") == space_id:
                ids.add(identifier)
                del run_repo._runs[identifier]
        for key, record in list(materials.idempotency.items()):
            if record[1] in ids:
                materials.idempotency_responses[key] = {"resource_deleted": True}
                materials.idempotency[key] = (record[0], record[1], None)

    def _sql(self, space_id, assessments, plans, sessions, ids, run_ids):
        with self.uow.session() as session:
            for row in session.scalars(select(ExportRow).where(ExportRow.space_id == space_id).with_for_update()):
                ids.add(row.id)
                run_ids.add(row.run_id)
            for row in session.scalars(select(KnowledgeCorrectionRow).where(KnowledgeCorrectionRow.space_id == space_id).with_for_update()):
                ids.add(row.id)
            session.execute(delete(ExportRow).where(ExportRow.space_id == space_id))
            session.execute(delete(KnowledgeCorrectionRow).where(KnowledgeCorrectionRow.space_id == space_id))
            for table, field, values in self._filters(space_id, assessments, plans, sessions):
                cls = TABLES[table]
                predicate = getattr(cls, field).in_(values)
                ids.update(session.scalars(select(cls.id).where(predicate).with_for_update()))
                session.execute(delete(cls).where(predicate))
            run_ids.update(session.scalars(select(RunRow.id).where(or_(RunRow.result_ref["id"].as_string().in_(ids), RunRow.result_ref["space_id"].as_string() == space_id)).with_for_update()))
            ids.update(run_ids)
            session.execute(delete(OutboxEventRow).where(OutboxEventRow.aggregate_id.in_(ids)))
            session.execute(delete(RunEventRow).where(RunEventRow.run_id.in_(run_ids)))
            session.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))
            for row in session.scalars(select(IdempotencyRow).where(IdempotencyRow.resource_id.in_(ids)).with_for_update()):
                row.response = {"resource_deleted": True}
                row.run_id = row.version_id = None
