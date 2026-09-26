"""Read-only budget-comparison previews over one locked learner snapshot."""
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.plans.comparison import PlanComparisonService
from knowpath_backend.learning.plans.comparison_schemas import ComparePlans
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error, _error_response

router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/plan-comparisons")
async def compare_plans(state: LearningStateDep, space_id: str, payload: ComparePlans,
                        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    if not idempotency_key or not idempotency_key.strip():
        return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "预算比较必须提供 Idempotency-Key")
    try:
        result = PlanComparisonService(state.plan_sessions).compare(space_id, payload.model_dump(), idempotency_key)
        return JSONResponse(status_code=200, content=result)
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
