"""Commit material ingestion and its replayable Run in one transaction."""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, OperationalError

from .materials import CreateMaterialResult, MaterialService
from .runs import Run, RunService


@dataclass(frozen=True)
class IngestionResult:
    resource: CreateMaterialResult
    run: Run


class MaterialIngestionService:
    def __init__(self, materials: MaterialService, runs: RunService):
        self.materials = materials
        self.runs = runs

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
                with repository.transaction(), self.runs.repository.transaction():
                    resource = operation(**kwargs)
                    run_id = repository.get_idempotency_run(key)
                    if run_id is None:
                        run = self.runs.create("material_ingest", {"type": "material_version", "id": resource.version.id},
                                               status="succeeded")
                        repository.bind_idempotency_run(key, run["id"])
                        run_id = run["id"]
                # Read after commit so a concurrent legacy repair's Run and
                # events are visible outside the earlier MySQL read snapshot.
                return IngestionResult(resource, self.runs.get(run_id))
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
