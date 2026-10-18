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
    schedule_mode: Literal['session_window', 'budget_rolling'] | None = None
    daily_minutes: int = Field(default=30, ge=1, le=480)
    horizon_days: int = Field(default=7, ge=1, le=30)
    calendar_timezone: str = Field(default='Asia/Shanghai', min_length=1, max_length=64)
    session_count: int = Field(default=3, ge=3, le=5)
    minutes_per_session: int = Field(default=30, ge=10, le=120)
    include_review: bool = True
    order_mode: Literal["source", "adaptive"] | None = None
    material_order: list[str] | None = None
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

class UndoTaskCompletion(StrictPlanRequest):
    expected_plan_version: int = Field(ge=1)
    completion_event_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2000)


class UpdateTask(StrictPlanRequest):
    expected_plan_version: int = Field(ge=1)
    status: Literal["completed", "skipped", "deferred"]
    note: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    defer_until: datetime | None = None
    source: Literal["manual", "assessment"] = "manual"
    assessment_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def status_fields(self):
        if self.source == "assessment" and (self.status != "completed" or self.assessment_id is None):
            raise ValueError("测评完成确认必须提供 assessment_id 且 status 为 completed")
        if self.source == "manual" and self.assessment_id is not None:
            raise ValueError("手工确认不能提供 assessment_id")
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


class OrderPlan(StrictPlanRequest):
    expected_plan_version: int = Field(ge=1)
    order_mode: Literal["source", "adaptive", "custom"]
    material_order: list[str] | None = None
    ordered_node_ids: list[str] | None = None

    @model_validator(mode="after")
    def custom_fields(self):
        if self.order_mode == "custom" and self.ordered_node_ids is None:
            raise ValueError("custom 必须提供 ordered_node_ids")
        if self.order_mode != "custom" and self.ordered_node_ids is not None:
            raise ValueError("只有 custom 可以提供 ordered_node_ids")
        return self
