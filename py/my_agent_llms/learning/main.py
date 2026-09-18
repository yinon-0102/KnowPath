"""ASGI entrypoint for local development: ``uvicorn my_agent_llms.learning.main:app``."""

import os
from dotenv import load_dotenv

from .api import create_app
from .config import server_settings
from .materials import MaterialService


def build_app():
    load_dotenv()
    settings = server_settings()
    if os.getenv("LEARNING_PERSISTENCE", "memory").lower() == "sql":
        from .db import create_db_engine
        from .repositories import SqlAlchemyMaterialRepository
        from .run_repository import SqlAlchemyRunRepository
        from .runs import RunService

        engine = create_db_engine()
        # Apply Alembic migrations before starting this single-process runtime.
        materials = SqlAlchemyMaterialRepository(engine)
        runs = RunService(SqlAlchemyRunRepository(engine, unit_of_work=materials.unit_of_work))
        from .learning_repository import SqlAlchemyLearningRepository
        from .startup import recover_legacy_runs
        recover_legacy_runs(runs, SqlAlchemyLearningRepository(materials.unit_of_work))
        return create_app(MaterialService(materials), run_service=runs, settings=settings)
    return create_app(settings=settings)


app = build_app()

__all__ = ["app", "build_app"]
