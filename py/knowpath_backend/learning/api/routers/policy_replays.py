"""Read-only historical topic policy replay and observed retests."""
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from knowpath_backend.learning.evaluation.replay import ReplayService
from knowpath_backend.learning.evaluation.schemas import ReplayRequest
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error, _error_response

router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/policy-replays")
def replay_policy(state: LearningStateDep, space_id: str, payload: ReplayRequest,
                  idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    if not idempotency_key or not idempotency_key.strip():
        return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "回放必须提供 Idempotency-Key")
    try:
        return JSONResponse(status_code=200, content=ReplayService(state.assessment_service).replay(
            space_id, payload.model_dump(), idempotency_key))
    except (DomainConflict, DomainNotFound) as exc:
        return _domain_error(exc)
