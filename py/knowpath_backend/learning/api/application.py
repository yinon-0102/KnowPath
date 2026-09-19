"""Compose one learning application and its owned runtime resources."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import LearningSettings
from knowpath_backend.learning.materials.ingestion import MaterialIngestionService
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository, MaterialService
from knowpath_backend.learning.conversations.generation import DashScopeAnswerGenerator
from knowpath_backend.learning.assessments.generation import DashScopeQuestionGenerator
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.persistence.run_repository import SqlAlchemyRunRepository
from knowpath_backend.learning.workers.runs import RunService
from knowpath_backend.learning.materials.source_access import SourceAccessService
from ..state import LearningState
from knowpath_backend.learning.rag.retrieval import configured_retriever
from .contract import ContractRoute
from .errors import install_error_handlers
from .middleware import install_middleware
from .routers import health, materials, graphs, runs, spaces, assessments, plans_sessions, messages, knowledge, exports


def create_app(
    service: MaterialService | None = None,
    settings: LearningSettings | None = None,
    *,
    run_service: RunService | None = None,
    question_generator=None,
    answer_generator=None,
    source_retriever=None,
) -> FastAPI:
    service = service or MaterialService(InMemoryMaterialRepository())
    settings = settings or LearningSettings.from_env()
    if run_service is None and isinstance(service.repository, SqlAlchemyMaterialRepository):
        uow = service.repository.unit_of_work
        run_service = RunService(SqlAlchemyRunRepository(uow.engine, unit_of_work=uow))
    if run_service is None and isinstance(service.repository, InMemoryMaterialRepository):
        run_service = RunService(service.repository.run_repository)
    state = LearningState(material_repository=service.repository, run_service=run_service,
                          context_settings=settings,
                          question_generator=question_generator if question_generator is not None else DashScopeQuestionGenerator(settings),
                          answer_generator=answer_generator if answer_generator is not None else DashScopeAnswerGenerator(settings),
                          source_retriever=source_retriever if source_retriever is not None else configured_retriever(settings))
    ingestion = MaterialIngestionService(service, state.run_service, state.graph_service)
    source_access = SourceAccessService(state.assessment_service)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if source_retriever is None:
                close = getattr(state.message_service.retriever, "close", None)
                if close is not None:
                    close()

    app = FastAPI(title="Keel Learning", version="0.1.0", lifespan=lifespan)
    app.state.learning_state = state
    app.router.route_class = ContractRoute
    app.state.material_service = service
    app.state.settings = settings
    app.state.ingestion_service = ingestion
    app.state.source_access_service = source_access
    install_middleware(app, settings)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(materials.router)
    app.include_router(graphs.router)
    app.include_router(runs.router)
    app.include_router(spaces.router)
    app.include_router(assessments.router)
    app.include_router(plans_sessions.router)
    app.include_router(messages.router)
    app.include_router(knowledge.router)
    app.include_router(exports.router)
    return app
