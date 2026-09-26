"""Public diagnostic decisions without exposing the frozen candidate pool."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error

router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/assessments/{assessment_id}/diagnostic")
def get_diagnostic(state: LearningStateDep, assessment_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.assessment_service.diagnostic(assessment_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
