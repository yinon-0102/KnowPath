"""HTTP routes for health."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter
from fastapi import Request

from ..contract import ContractRoute
from ..dependencies import MaterialServiceDep, SettingsDep


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/health")
async def health(service: MaterialServiceDep, settings: SettingsDep, request: Request) -> dict:
    if settings.local_token and not hmac.compare_digest(request.headers.get("X-Local-Token", "").encode("utf-8"), settings.local_token.encode("utf-8")):
        return {"status": "ok"}
    from ...health import report
    return await asyncio.to_thread(report, service.repository, settings)
