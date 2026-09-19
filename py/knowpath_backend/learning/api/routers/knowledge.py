"""HTTP routes for knowledge."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from fastapi import Header, Query
from fastapi.responses import JSONResponse

from ...assessment_schemas import ResetState
from ...correction_schemas import CreateCorrection, ConfirmCorrection
from ...errors import DomainConflict, DomainNotFound
from ...knowledge_updates import ApplyKnowledgeUpdates
from ...pagination import page_records
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/learning-spaces/{space_id}/evidence")
def list_evidence(state: LearningStateDep, space_id: str, topic_id: str | None = None, kind: str | None = None,
                  from_time: datetime | None = Query(None, alias="from"),
                  to_time: datetime | None = Query(None, alias="to"),
                  limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=2048)) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.assessment_service.evidence_page(space_id,
            topic_id=topic_id, kind=kind, from_time=from_time, to_time=to_time, limit=limit, cursor=cursor))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces/{space_id}/knowledge-corrections", status_code=201)
async def create_knowledge_correction(state: LearningStateDep, space_id: str, payload: CreateCorrection,
                                      idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=201, content=state.create_correction(space_id, payload.model_dump(), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces/{space_id}/knowledge-corrections/{correction_id}/confirm", status_code=202)
async def confirm_knowledge_correction(state: LearningStateDep, space_id: str, correction_id: str, payload: ConfirmCorrection,
                                       idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.confirm_correction(space_id, correction_id, payload.model_dump(), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/learning-spaces/{space_id}/changes")
async def list_changes(state: LearningStateDep, space_id: str, cursor: str | None = Query(None, max_length=2048),
                       limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    try:
        state.get_space(space_id)
        return JSONResponse(content=page_records(state.changes_for(space_id), scope=["changes", space_id], limit=limit, cursor=cursor))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/learning-spaces/{space_id}/knowledge-updates")
async def knowledge_updates(state: LearningStateDep, space_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.knowledge_updates(space_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces/{space_id}/knowledge-updates/apply", status_code=202)
async def apply_knowledge_updates(state: LearningStateDep, space_id: str, payload: ApplyKnowledgeUpdates, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.apply_knowledge_updates(space_id, payload.model_dump(), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces/{space_id}/state/reset")
def reset_learning_state(state: LearningStateDep, space_id: str, payload: ResetState, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.reset_state(space_id, payload.topic_ids, payload.reason, expected_state_version=payload.expected_state_version, idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
