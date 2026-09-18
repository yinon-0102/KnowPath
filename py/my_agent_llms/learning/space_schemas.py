"""Validated transport contracts for learning-space writes."""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Name = Annotated[str, Field(min_length=1, max_length=100)]
Goal = Annotated[str, Field(min_length=1, max_length=1000)]
Budget = Annotated[int, Field(strict=True, ge=15, le=2400)]
Version = Annotated[int, Field(strict=True, ge=1)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Preferences(Contract):
    example_first: StrictBool | None = None
    concise_explanations: StrictBool | None = None


class DatedContract(Contract):
    target_date: str | None = None

    @field_validator("target_date")
    @classmethod
    def valid_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("target_date must use YYYY-MM-DD")
        return value


class CreateSpace(DatedContract):
    name: Name = "未命名学习空间"
    material_ids: Annotated[list[Identifier], Field(min_length=1)]
    goal: Goal | None = None
    weekly_minutes: Budget | None = None


class UpdateSpace(Contract):
    name: Name | None = None
    status: Literal["active", "archived"] | None = None
    expected_version: Version

    @model_validator(mode="after")
    def non_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set - {"expected_version"}):
            raise ValueError("name and status cannot be null")
        return self


class SetScope(Contract):
    topic_ids: Annotated[list[Identifier], Field(min_length=1)]
    excluded_topic_ids: list[Identifier] = Field(default_factory=list)
    include_prerequisites: StrictBool = True
    expected_version: Version


class UpdateProfile(DatedContract):
    goal: Goal | None = None
    weekly_minutes: Budget | None = None
    preferences: Preferences | None = None
    expected_version: Version

    @model_validator(mode="after")
    def required_values(self):
        if any(field in self.model_fields_set and getattr(self, field) is None for field in ("goal", "weekly_minutes")):
            raise ValueError("goal and weekly_minutes cannot be null")
        return self


class DeleteSpace(Contract):
    expected_version: Version
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError("confirm must be true")
        return value
