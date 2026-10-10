"""Cloud-backed notebook reading, editing and generation endpoints."""
from typing import Annotated, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error
from ...errors import DomainConflict, DomainNotFound


def response(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (DomainConflict, DomainNotFound) as exc:
        return _domain_error(exc)


class BlockEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str | None = None
    kind: Literal["assessment", "personal"] | None = None
    markdown: str = Field(max_length=200000)


class CorrectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    markdown: str = Field(max_length=200000)


class ChapterEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    blocks: list[BlockEdit] = Field(default_factory=list, max_length=1000)
    corrections: list[CorrectionEdit] = Field(default_factory=list, max_length=1000)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/notebooks")
def notebooks(state: LearningStateDep):
    return response(state.notes_service.list_notebooks)


@router.get("/api/v1/learning-spaces/{space_id}/notebook")
def notebook(space_id: str, state: LearningStateDep):
    return response(state.notes_service.notebook, space_id)


@router.get("/api/v1/note-chapters/{chapter_id}")
def chapter(chapter_id: str, state: LearningStateDep):
    return response(state.notes_service.chapter, chapter_id)


@router.patch("/api/v1/note-chapters/{chapter_id}")
def edit_chapter(chapter_id: str, payload: ChapterEdit, state: LearningStateDep):
    return response(state.notes_service.edit, chapter_id, payload.model_dump(exclude_none=True))


@router.get("/api/v1/note-chapters/{chapter_id}/revisions")
def revisions(chapter_id: str, state: LearningStateDep):
    return response(state.notes_service.revisions, chapter_id)


@router.get("/api/v1/note-chapters/{chapter_id}/revisions/{revision_id}")
def revision(chapter_id: str, revision_id: str, state: LearningStateDep):
    return response(state.notes_service.revision, chapter_id, revision_id)


@router.post("/api/v1/note-generations/{generation_id}/retry", status_code=202)
def retry(generation_id: str, payload: RetryRequest, state: LearningStateDep,
          key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    return response(state.notes_service.retry, generation_id, key=key)
