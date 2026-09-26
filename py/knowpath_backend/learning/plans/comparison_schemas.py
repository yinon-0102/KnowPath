"""Strict, bounded request contract for read-only budget comparisons."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ComparePlans(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    budgets_minutes_per_day: list[Annotated[int, Field(strict=True, ge=1, le=480)]] = Field(min_length=2, max_length=5)
    horizon_days: int = Field(default=7, ge=1, le=30)

    @field_validator("budgets_minutes_per_day")
    @classmethod
    def distinct_budgets(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("每日预算必须互不相同")
        return values
