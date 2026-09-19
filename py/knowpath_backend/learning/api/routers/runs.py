"""HTTP routes for runs."""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...errors import DomainConflict, DomainNotFound
from ...planner_schemas import EmptyObject
from ...runs import stream_run_events
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error, _error_response


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/runs/{run_id}")
def get_run(state: LearningStateDep, run_id: str) -> JSONResponse:
    try:
        run = state.get_run(run_id)
        return JSONResponse(status_code=200, content={k: v for k, v in run.items() if k != "events"})
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.get("/api/v1/runs/{run_id}/events", response_model=None,
         responses={200: {"content": {"text/event-stream": {}}}})
async def get_run_events(state: LearningStateDep, run_id: str, request: Request) -> StreamingResponse | JSONResponse:
    cursor = request.headers.get("Last-Event-ID", "0")
    if not re.fullmatch(r"[0-9]{1,20}", cursor):
        return _error_response(422, "INVALID_EVENT_ID", "Last-Event-ID 须为非负整数")
    last_event_id = int(cursor)
    try:
        # Validate before sending HTTP headers so missing/expired history is JSON.
        await asyncio.to_thread(state.events_for, run_id, after_id=last_event_id)
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
    return StreamingResponse(
        stream_run_events(state.run_service, run_id, after_id=last_event_id,
                          is_disconnected=request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/runs/{run_id}/cancel", status_code=202)
def cancel_run(state: LearningStateDep, run_id: str, _: EmptyObject) -> JSONResponse:
    try:
        run = state.cancel_run(run_id)
        status_code = 200 if run["status"] in {"succeeded", "failed", "cancelled"} else 202
        return JSONResponse(status_code=status_code, content={"id": run_id, "status": run["status"]})
    except DomainNotFound as exc:
        return _domain_error(exc)
