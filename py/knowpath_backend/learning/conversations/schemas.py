"""Strict message transport contract."""
from typing import Annotated, Literal
from pydantic import Field, StrictBool, field_validator, model_validator
from knowpath_backend.learning.spaces.schemas import Contract, Identifier


class ReadingContext(Contract):
    material_id: Identifier
    material_version_id: Identifier


class SendMessage(Contract):
    answer_mode: Literal["auto", "material", "general"] = "auto"

    @field_validator("answer_mode", mode="before")
    @classmethod
    def default_answer_mode(cls, value):
        return "auto" if value is None else value

    lesson_id: Identifier | None = None
    reading_context: ReadingContext | None = None
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    session_id: Identifier | None = None
    stream: StrictBool = True
    material_ids: Annotated[list[Identifier], Field(min_length=1, max_length=5)] | None = None

    @model_validator(mode='after')
    def exclusive_context(self):
        if self.lesson_id is not None and self.reading_context is not None:
            raise ValueError('lesson_id and reading_context are mutually exclusive')
        return self

    @field_validator('material_ids')
    @classmethod
    def canonical_material_ids(cls, value):
        return sorted(set(value)) if value is not None else None

    @field_validator("stream")
    @classmethod
    def streaming_only(cls, value):
        if value is not True:
            raise ValueError("only stream=true is supported")
        return value
