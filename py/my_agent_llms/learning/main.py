"""ASGI entrypoint for local development: ``uvicorn my_agent_llms.learning.main:app``."""

import os

from .api import create_app
from .materials import MaterialService


def build_app():
    if os.getenv("LEARNING_PERSISTENCE", "memory").lower() == "sql":
        from .db import create_db_engine, init_db
        from .repositories import SqlAlchemyMaterialRepository

        engine = create_db_engine()
        init_db(engine)
        return create_app(MaterialService(SqlAlchemyMaterialRepository(engine)))
    return create_app()


app = build_app()

__all__ = ["app", "build_app"]
