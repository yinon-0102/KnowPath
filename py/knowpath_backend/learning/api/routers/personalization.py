"""Versioned schedule application, activity and recommendation contracts."""
from typing import Annotated, Literal
from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from .notes import response


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Schedule(Request):
    comparison_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    expected_plan_version: int = Field(ge=1)
    expected_snapshot_hash: str = Field(min_length=1)


class Refresh(Request):
    expected_plan_version: int = Field(ge=1)


class Revert(Refresh):
    apply: bool = False


class ApplyRecommendation(Request):
    expected_plan_version: int | None = Field(default=None, ge=1)
    preview: bool = True


class Activity(Request):
    event: Literal['heartbeat','pause','resume','hidden','visible','disconnect','reconnect','waiting','ready'] = 'heartbeat'


class AnswerActivity(Request):
    question_id: str = Field(min_length=1)
    event: Literal['shown','heartbeat','pause','resume','hidden','visible','disconnect','reconnect','waiting','ready'] = 'heartbeat'


Key = Annotated[str | None, Header(alias='Idempotency-Key')]
router = APIRouter(route_class=ContractRoute)


@router.patch('/api/v1/plans/{plan_id}/schedule')
def schedule(plan_id: str, payload: Schedule, state: LearningStateDep, key: Key = None):
    return response(state.plan_adjustments.apply_schedule, plan_id, payload.model_dump(), key)


@router.post('/api/v1/plans/{plan_id}/refresh', status_code=202)
def refresh(plan_id: str, payload: Refresh, state: LearningStateDep, key: Key = None):
    return response(state.plan_adjustments.refresh, plan_id, payload.model_dump(), key)


@router.get('/api/v1/plans/{plan_id}/adjustments')
def adjustments(plan_id: str, state: LearningStateDep):
    return response(state.plan_adjustments.history, plan_id)


@router.post('/api/v1/plan-adjustments/{identifier}/revert')
def revert(identifier: str, payload: Revert, state: LearningStateDep, key: Key = None):
    return response(state.plan_adjustments.revert, identifier, payload.model_dump(), key)


@router.post('/api/v1/learning-spaces/{space_id}/policy-recommendations')
def recommendations(space_id: str, payload: Request, state: LearningStateDep, key: Key = None):
    return response(state.policy_recommendations.create, space_id, payload.model_dump(), key)


@router.post('/api/v1/policy-recommendations/{identifier}/apply')
def apply_recommendation(identifier: str, payload: ApplyRecommendation, state: LearningStateDep, key: Key = None):
    return response(state.policy_recommendations.apply, identifier, payload.model_dump(exclude_none=True), key)


@router.post('/api/v1/sessions/{session_id}/activity')
def activity(session_id: str, payload: Activity, state: LearningStateDep, key: Key = None):
    return response(state.plan_sessions.activity, session_id, payload.model_dump(), key)


@router.post('/api/v1/assessments/{assessment_id}/activity')
def answer_activity(assessment_id: str, payload: AnswerActivity, state: LearningStateDep, key: Key = None):
    return response(state.assessment_service.activity, assessment_id, payload.model_dump(), key)
