"""Graph command replay and history cleanup share the material transaction."""
import copy
from datetime import datetime, timezone
from sqlalchemy import delete, select
from .db import GraphRevisionRow, IdempotencyRow, OutboxEventRow, MaterialVersionRow
from .errors import DomainConflict
from .learning_repository import InMemoryLearningRepository, SqlAlchemyLearningRepository


class InMemoryGraphRepository(InMemoryLearningRepository):
    def delete_history(self, material_id, revision_ids):
        with self.materials._lock:
            tables = self.materials.assessment_data
            for identifier in revision_ids:
                tables["graph_revisions"].pop(identifier, None)
            tables["outbox"] = {key: row for key, row in tables["outbox"].items()
                               if not ((row["aggregate_type"] == "graph_revision" and row["aggregate_id"] in revision_ids)
                                       or (row["event_type"] == "material.parse" and row["payload"]["material_id"] == material_id))}
            for key, record in self.materials.idempotency.items():
                response = self.materials.idempotency_responses.get(key, {})
                if record[1] == material_id and (response.get("candidate_revision_id") in revision_ids or "run_id" in response):
                    self.materials.idempotency_responses[key] = {"resource_deleted": True}


class SqlAlchemyGraphRepository(SqlAlchemyLearningRepository):
    def replay(self, key, fingerprint):
        with self.unit_of_work.session() as session:
            row = session.get(IdempotencyRow, key)
            if row is None:
                return None
            if row.request_fingerprint != fingerprint or row.resource_type != "graph_command" or row.response is None:
                raise DomainConflict("IDEMPOTENCY_CONFLICT", "相同幂等键已用于不同请求")
            if row.response.get("resource_deleted"):
                raise DomainConflict("RESOURCE_DELETED", "资源已删除，原请求不可重放")
            return copy.deepcopy(row.response)

    def remember(self, key, fingerprint, resource_id, response):
        with self.unit_of_work.session() as session:
            session.add(IdempotencyRow(key=key, request_fingerprint=fingerprint,
                resource_type="graph_command", resource_id=resource_id,
                response=copy.deepcopy(response), created_at=datetime.now(timezone.utc)))

    def delete_history(self, material_id, revision_ids):
        with self.unit_of_work.session() as session:
            session.execute(delete(OutboxEventRow).where(OutboxEventRow.aggregate_type == "graph_revision",
                                                        OutboxEventRow.aggregate_id.in_(revision_ids)))
            session.execute(delete(OutboxEventRow).where(OutboxEventRow.event_type == "material.parse",
                OutboxEventRow.aggregate_id.in_(select(MaterialVersionRow.id).where(MaterialVersionRow.material_id == material_id))))
            session.execute(delete(GraphRevisionRow).where(GraphRevisionRow.material_id == material_id))
            for row in session.scalars(select(IdempotencyRow).where(IdempotencyRow.resource_type == "graph_command",
                                       IdempotencyRow.resource_id == material_id).with_for_update()):
                row.response = {"resource_deleted": True}
