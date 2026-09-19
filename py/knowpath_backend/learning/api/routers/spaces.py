"""HTTP routes for spaces."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Header, Query
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from ...pagination import page_records
from knowpath_backend.learning.spaces.schemas import CreateSpace, UpdateSpace, SetScope, UpdateProfile, DeleteSpace
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/learning-spaces")
def list_learning_spaces(state: LearningStateDep, cursor: str | None = Query(None, max_length=2048),
                         limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    try:
        return JSONResponse(content=page_records(state.list_spaces(), scope=["spaces"], limit=limit, cursor=cursor))
    except DomainConflict as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces", status_code=201)
def create_learning_space(state: LearningStateDep, payload: CreateSpace, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=201, content=state.create_space(payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key, require_published=True))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.patch("/api/v1/learning-spaces/{space_id}")
def update_learning_space(state: LearningStateDep, space_id: str, payload: UpdateSpace, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.update_space(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/learning-spaces/{space_id}")
def get_learning_space(state: LearningStateDep, space_id: str) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.get_space(space_id))
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.post("/api/v1/learning-spaces/{space_id}/scope")
def set_learning_scope(state: LearningStateDep, space_id: str, payload: SetScope, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.set_scope(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/learning-spaces/{space_id}/profile")
def get_learning_profile(state: LearningStateDep, space_id: str) -> JSONResponse:
    try:
        space = state.get_space(space_id)
        from knowpath_backend.learning.spaces.profile_candidates import candidates_for
        return JSONResponse(status_code=200, content={"profile": space["profile"], "profile_version": space["profile_version"],
            "candidates": candidates_for(state.assessment_service.repository, space_id)})
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.patch("/api/v1/learning-spaces/{space_id}/profile")
def update_learning_profile(state: LearningStateDep, space_id: str, payload: UpdateProfile, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.update_profile(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/learning-spaces/{space_id}/state")
def get_learning_state(state: LearningStateDep, space_id: str, topic_id: str | None = None, status: str | None = None, include_evidence: bool = False) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.get_state(space_id, topic_id=topic_id, status=status, include_evidence=include_evidence))
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.delete("/api/v1/learning-spaces/{space_id}", status_code=202)
def delete_learning_space(state: LearningStateDep, space_id: str, payload: DeleteSpace) -> JSONResponse:
    try:
        result = state.delete_space(space_id, payload.model_dump())
        return JSONResponse(status_code=202, content=result)
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
