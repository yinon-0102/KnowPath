"""Bounded report requests; timestamps must specify their timezone."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=200, ge=1, le=1000)
    minimum_delay_hours: int = Field(default=24, ge=24, le=8760)
    from_time: str | None = None
    to_time: str | None = None

    @field_validator("from_time", "to_time")
    @classmethod
    def aware_timestamp(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or "T" not in value:
                raise ValueError("时间必须使用带时区的 ISO 8601 格式")
        return value

    @model_validator(mode="after")
    def ordered_range(self):
        if self.from_time and self.to_time and datetime.fromisoformat(self.from_time) > datetime.fromisoformat(self.to_time):
            raise ValueError("开始时间不得晚于结束时间")
        return self
