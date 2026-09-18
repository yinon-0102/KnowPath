"""Strict request contracts for assessment commands."""
from typing import Annotated, Literal
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class DifficultyMix(StrictRequest):
    easy: float = Field(ge=0, le=1)
    medium: float = Field(ge=0, le=1)
    hard: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def total(self):
        if not math.isclose(self.easy + self.medium + self.hard, 1.0, abs_tol=1e-8):
            raise ValueError("difficulty_mix 合计须为 1")
        return self


class CreateAssessment(StrictRequest):
    kind: Literal["diagnostic", "practice", "retest"] = "diagnostic"
    topic_ids: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, min_length=1)
    question_count: int = Field(default=5, ge=5, le=10)
    question_types: list[Literal["single_choice", "short_answer"]] = Field(default_factory=lambda: ["single_choice"], min_length=1, max_length=2)
    difficulty_mix: DifficultyMix | None = None

    @model_validator(mode="after")
    def unique(self):
        for items in (self.topic_ids or [], self.question_types):
            if len(items) != len(set(items)):
                raise ValueError("题型和主题不可重复")
        return self


class Answer(StrictRequest):
    question_id: str = Field(min_length=1)
    expected_answer_revision: int = Field(ge=0)
    answer: str = Field(min_length=1, max_length=4000)
    elapsed_seconds: int | None = Field(default=None, ge=0, le=86400)


class RecordAttempt(StrictRequest):
    answers: list[Answer] = Field(min_length=1, max_length=10)
    finalize: bool = False

    @model_validator(mode="after")
    def unique(self):
        if len({a.question_id for a in self.answers}) != len(self.answers):
            raise ValueError("同一批次 question_id 不可重复")
        return self


class FinalizeAssessment(StrictRequest):
    allow_unanswered: bool = False


class ResetState(StrictRequest):
    topic_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    expected_state_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.topic_ids)) != len(self.topic_ids):
            raise ValueError("topic_ids 不可重复")
        return self
