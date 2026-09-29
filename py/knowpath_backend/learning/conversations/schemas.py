"""Strict message transport contract."""
from typing import Annotated
from pydantic import Field, StrictBool, field_validator
from knowpath_backend.learning.spaces.schemas import Contract, Identifier


class SendMessage(Contract):
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    session_id: Identifier | None = None
    stream: StrictBool = True
    material_ids: Annotated[list[Identifier], Field(min_length=1, max_length=5)] | None = None

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
