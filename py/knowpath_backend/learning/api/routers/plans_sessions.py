"""HTTP routes for plans sessions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Header
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.plans.schemas import CreatePlan, UpdateTask, StartSession, SessionEvent, EmptyObject
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error, _error_response


router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/plans", status_code=202)
async def create_plan(state: LearningStateDep, space_id: str, payload: CreatePlan, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        plan = state.create_plan(space_id, payload.model_dump(mode="json", exclude_none=True), idempotency_key=idempotency_key)
        return JSONResponse(status_code=202, content={"run_id": plan["run_id"], "plan_id": plan["plan_id"], "status": plan["status"]})
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/plans/{plan_id}")
async def get_plan(state: LearningStateDep, plan_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.get_plan(plan_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.patch("/api/v1/plans/{plan_id}/tasks/{task_id}")
async def update_plan_task(state: LearningStateDep, plan_id: str, task_id: str, payload: UpdateTask) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.update_task(plan_id, task_id, payload.model_dump(mode="json", exclude_none=True)))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/plans/{plan_id}/sessions", status_code=201)
async def start_learning_session(state: LearningStateDep, plan_id: str, payload: StartSession, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=201, content=state.start_session(plan_id, payload.task_id, idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/sessions/{session_id}/events", status_code=201)
async def add_learning_event(state: LearningStateDep, session_id: str, payload: SessionEvent, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    if not (idempotency_key or payload.event_id):
        return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "会话事件必须提供 Idempotency-Key 或 event_id")
    try:
        return JSONResponse(status_code=201, content=state.add_session_event(session_id, payload.model_dump(mode="json", exclude_none=True), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/sessions/{session_id}/finish")
async def finish_learning_session(state: LearningStateDep, session_id: str, _: EmptyObject) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.finish_session(session_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
