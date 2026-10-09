"""ASGI entrypoint for local development: ``uvicorn knowpath_backend.learning.main:app``."""

import os
import logging
from knowpath_backend.observability import configure_logging, span, log_event
from knowpath_backend.core.env import load_project_env


from .api import create_app
from .config import server_settings
from knowpath_backend.learning.materials.service import MaterialService

logger = logging.getLogger(__name__)


def build_app():
    load_project_env()
    configure_logging('api')
    with span(logger, 'api.build'):
        return _build_app()


def _build_app():
    settings = server_settings()
    log_event(logger, 'api.configuration', persistence='sql' if os.getenv('LEARNING_PERSISTENCE', 'memory').lower() == 'sql' else 'memory',
              chat_provider=settings.chat_provider, chat_model=settings.chat_model,
              embedding_provider=settings.embedding_provider, embedding_model=settings.embedding_model)
    if os.getenv("LEARNING_PERSISTENCE", "memory").lower() == "sql":
        from knowpath_backend.learning.persistence.db import create_db_engine
        from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
        from knowpath_backend.learning.persistence.run_repository import SqlAlchemyRunRepository
        from knowpath_backend.learning.workers.runs import RunService

        engine = create_db_engine()
        # Apply Alembic migrations before starting this single-process runtime.
        materials = SqlAlchemyMaterialRepository.from_env(engine)
        runs = RunService(SqlAlchemyRunRepository(engine, unit_of_work=materials.unit_of_work))
        from knowpath_backend.learning.persistence.learning_repository import SqlAlchemyLearningRepository
        from .startup import recover_legacy_runs
        recover_legacy_runs(runs, SqlAlchemyLearningRepository(materials.unit_of_work))
        return create_app(MaterialService(materials), run_service=runs, settings=settings)
    return create_app(settings=settings)


app = build_app()

__all__ = ["app", "build_app"]
