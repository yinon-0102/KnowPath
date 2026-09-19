"""HTTP routes for exports."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from fastapi import Header
from fastapi.responses import JSONResponse, Response

from ...errors import DomainConflict, DomainNotFound
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error


router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/exports", status_code=202)
def create_export(state: LearningStateDep, space_id: str, payload: dict[str, Any], idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.create_export(space_id, payload, idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/exports/{export_id}/download")
def download_export(state: LearningStateDep, export_id: str) -> Response:
    try:
        return Response(content=state.export_archive(export_id), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="learning-space.zip"'})
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
