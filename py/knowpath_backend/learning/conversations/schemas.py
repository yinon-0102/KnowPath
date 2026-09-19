"""Strict message transport contract."""
from typing import Annotated
from pydantic import Field, StrictBool, field_validator
from knowpath_backend.learning.spaces.schemas import Contract, Identifier


class SendMessage(Contract):
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    session_id: Identifier | None = None
    stream: StrictBool = True

    @field_validator("stream")
    @classmethod
    def streaming_only(cls, value):
        if value is not True:
            raise ValueError("only stream=true is supported")
        return value
