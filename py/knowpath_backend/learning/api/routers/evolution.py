"""Read-only review-policy and evidence-evolution endpoint."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from knowpath_backend.learning.evolution.service import EvolutionService
from ...errors import DomainConflict, DomainNotFound
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/learning-spaces/{space_id}/evolution")
def evolution(state: LearningStateDep, space_id: str, limit: int = Query(default=200, ge=1, le=500)) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=EvolutionService(state.assessment_service).get(space_id, limit=limit))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
