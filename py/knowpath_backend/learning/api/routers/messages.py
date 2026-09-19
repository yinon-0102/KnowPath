"""HTTP routes for messages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import BackgroundTasks, Header
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.conversations.schemas import SendMessage
from ..contract import ContractRoute
from ..dependencies import LearningStateDep, SettingsDep
from ..errors import _domain_error, validate_chat


router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/messages", status_code=202)
def send_learning_message(settings: SettingsDep, state: LearningStateDep, space_id: str, payload: SendMessage, background_tasks: BackgroundTasks,
                          idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        validate_chat(state.message_service.generator, settings)
        if getattr(state.message_service.retriever, "configuration_error", None):
            raise DomainConflict("UNSUPPORTED_MODEL", "配置的向量模型不支持当前任务所需能力")
        return JSONResponse(status_code=202, content=state.send_message(space_id, payload.model_dump(), idempotency_key=idempotency_key,
                                                                       dispatch=background_tasks.add_task))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
