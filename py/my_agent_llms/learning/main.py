"""ASGI entrypoint for local development: ``uvicorn my_agent_llms.learning.main:app``."""

import os

from .api import create_app
from .materials import MaterialService


def build_app():
    if os.getenv("LEARNING_PERSISTENCE", "memory").lower() == "sql":
        from .db import create_db_engine
        from .repositories import SqlAlchemyMaterialRepository
        from .run_repository import SqlAlchemyRunRepository
        from .runs import RunService

        engine = create_db_engine()
        # Apply Alembic migrations before starting this single-process runtime.
        materials = SqlAlchemyMaterialRepository(engine)
        runs = RunService(SqlAlchemyRunRepository(engine, unit_of_work=materials.unit_of_work))
        runs.recover_interrupted(exclude_kinds={"graph_reconcile"})
        return create_app(MaterialService(materials), run_service=runs)
    return create_app()


app = build_app()

__all__ = ["app", "build_app"]
