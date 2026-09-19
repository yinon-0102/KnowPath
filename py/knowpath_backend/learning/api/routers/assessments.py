"""HTTP routes for assessments."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import BackgroundTasks, Header
from fastapi.responses import JSONResponse

from ...assessment_schemas import CreateAssessment, RecordAttempt, FinalizeAssessment, GradeReview
from ...errors import DomainConflict, DomainNotFound
from ..contract import ContractRoute
from ..dependencies import LearningStateDep, SettingsDep
from ..errors import _domain_error, validate_chat


router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/learning-spaces/{space_id}/assessments", status_code=202)
def create_assessment(settings: SettingsDep, state: LearningStateDep, space_id: str, payload: CreateAssessment, background_tasks: BackgroundTasks, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        validate_chat(state.assessment_service.generator, settings)
        assessment = state.create_assessment(space_id, payload.model_dump(exclude_none=True), idempotency_key=idempotency_key, dispatch=background_tasks.add_task, durable=True)
        return JSONResponse(status_code=202, content={"run_id": assessment["run_id"], "assessment_id": assessment["id"], "assessment": assessment, "status": assessment["status"]})
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/assessments/{assessment_id}")
def get_assessment(state: LearningStateDep, assessment_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.get_assessment(assessment_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/assessments/{assessment_id}/attempts")
def record_attempt(state: LearningStateDep, assessment_id: str, payload: RecordAttempt, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202 if payload.finalize else 200, content=state.record_attempt(assessment_id, payload.model_dump(), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/assessments/{assessment_id}/finalize", status_code=202)
def finalize_assessment(state: LearningStateDep, assessment_id: str, payload: FinalizeAssessment) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.finalize_assessment(assessment_id, payload.model_dump()))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/assessments/{assessment_id}/result")
def assessment_result(state: LearningStateDep, assessment_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.assessment_result(assessment_id))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/assessments/{assessment_id}/grade-reviews", status_code=202)
def grade_review(state: LearningStateDep, assessment_id: str, payload: GradeReview, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.grade_review(assessment_id, payload.model_dump(), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
