"""Commit material ingestion and its replayable Run in one transaction."""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, OperationalError

from knowpath_backend.learning.materials.service import CreateMaterialResult, MaterialService
from knowpath_backend.learning.workers.runs import Run, RunService


@dataclass(frozen=True)
class IngestionResult:
    resource: CreateMaterialResult
    run: Run | None


class MaterialIngestionService:
    def __init__(self, materials: MaterialService, runs: RunService, graph):
        self.materials = materials
        self.runs = runs
        self.graph = graph

    def create(self, **kwargs) -> IngestionResult:
        return self._execute(self.materials.create, kwargs)

    def create_version(self, **kwargs) -> IngestionResult:
        return self._execute(self.materials.create_version, kwargs)

    def _execute(self, operation, kwargs) -> IngestionResult:
        repository = self.materials.repository
        material_uow = getattr(repository, "unit_of_work", None)
        run_uow = getattr(self.runs.repository, "unit_of_work", None)
        if material_uow is not run_uow:
            raise ValueError("material and run repositories must share one transaction context")
        key = kwargs["idempotency_key"]
        for attempt in range(3):
            try:
                with self.graph.repository.transaction():
                    resource = operation(**kwargs, defer_parse=True)
                    run_id = repository.get_idempotency_run(key)
                    if run_id is None and kwargs.get("auto_ingest", True):
                        # Existing upload keys replay their Run; deferred uploads have none.
                        response = self.graph.enqueue_ingest(resource.material.id, resource.version.id)
                        run_id = response["run_id"]
                        repository.bind_idempotency_run(key, run_id)
                    resource = CreateMaterialResult(repository.get_material(resource.material.id),
                                                    repository.get_version(resource.version.id), resource.replayed)
                # Read after commit so a concurrent legacy repair's Run and
                # events are visible outside the earlier MySQL read snapshot.
                return IngestionResult(resource, self.runs.get(run_id) if run_id else None)
            except IntegrityError:
                # A concurrent request may win the unique idempotency-key insert.
                # The whole losing transaction has rolled back before replaying.
                if attempt == 2 or repository.get_idempotency(key) is None:
                    raise
            except OperationalError as exc:
                # MySQL can abort a transaction while resolving contended locks.
                code = exc.orig.args[0] if exc.orig.args else None
                if attempt == 2 or code not in {1205, 1213}:
                    raise
        raise AssertionError("unreachable")
