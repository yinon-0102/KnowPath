"""Strict transport contracts for study plans and learning sessions."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

class StrictPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    @field_validator("defer_until", "client_timestamp", mode="before", check_fields=False)
    @classmethod
    def iso_timestamp(cls, value):
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("时间必须包含时区")
            return parsed
        return value

class CreatePlan(StrictPlanRequest):
    session_count: int = Field(default=3, ge=3, le=5)
    minutes_per_session: int = Field(default=30, ge=10, le=120)
    include_review: bool = True
    rebuild_mode: Literal["initial", "local_replan"] = "initial"
    base_plan_id: str | None = Field(default=None, min_length=1)
    expected_plan_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def replan_fields(self):
        if self.rebuild_mode == "local_replan" and (self.base_plan_id is None or self.expected_plan_version is None):
            raise ValueError("local_replan 必须提供 base_plan_id 和 expected_plan_version")
        if self.rebuild_mode == "initial" and (self.base_plan_id is not None or self.expected_plan_version is not None):
            raise ValueError("initial 不应提供 base_plan_id 或 expected_plan_version")
        return self

class UpdateTask(StrictPlanRequest):
    expected_plan_version: int = Field(ge=1)
    status: Literal["completed", "skipped", "deferred"]
    note: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    defer_until: datetime | None = None

    @model_validator(mode="after")
    def status_fields(self):
        if self.status == "skipped" and not self.reason:
            raise ValueError("skipped 必须提供 reason")
        if self.status == "deferred":
            if self.defer_until is None or self.defer_until.tzinfo is None or self.defer_until.utcoffset().total_seconds() != 0:
                raise ValueError("deferred 必须提供 UTC defer_until")
        elif self.defer_until is not None:
            raise ValueError("只有 deferred 可以提供 defer_until")
        return self

class EmptyObject(StrictPlanRequest):
    pass

class StartSession(StrictPlanRequest):
    task_id: str = Field(min_length=1)

class SessionEvent(StrictPlanRequest):
    type: Literal["open_material", "request_explanation", "request_hint", "pause", "resume"]
    topic_id: str | None = Field(default=None, min_length=1)
    question_id: str | None = Field(default=None, min_length=1)
    client_timestamp: datetime | None = None
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
