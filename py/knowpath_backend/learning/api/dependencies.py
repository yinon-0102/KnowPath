"""Resolve per-application services without opening new units of work."""

from typing import Annotated

from fastapi import Depends, Request

from ..config import LearningSettings
from knowpath_backend.learning.materials.ingestion import MaterialIngestionService
from knowpath_backend.learning.materials.service import MaterialService
from knowpath_backend.learning.materials.source_access import SourceAccessService
from ..state import LearningState


def get_material_service(request: Request) -> MaterialService:
    return request.app.state.material_service


def get_settings(request: Request) -> LearningSettings:
    return request.app.state.settings


def get_learning_state(request: Request) -> LearningState:
    return request.app.state.learning_state


def get_ingestion_service(request: Request) -> MaterialIngestionService:
    return request.app.state.ingestion_service


def get_source_access(request: Request) -> SourceAccessService:
    return request.app.state.source_access_service


MaterialServiceDep = Annotated[MaterialService, Depends(get_material_service)]
SettingsDep = Annotated[LearningSettings, Depends(get_settings)]
LearningStateDep = Annotated[LearningState, Depends(get_learning_state)]
IngestionServiceDep = Annotated[MaterialIngestionService, Depends(get_ingestion_service)]
SourceAccessDep = Annotated[SourceAccessService, Depends(get_source_access)]
