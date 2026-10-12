"""Study lessons generated only on explicit demand."""
from typing import Annotated, Literal
from fastapi import APIRouter, Header, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from .notes import response, RetryRequest
from ...workers.model_tasks import ModelTaskWorker

class CreateLesson(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    plan_id: str=Field(min_length=1,max_length=128)
    task_id: str=Field(min_length=1,max_length=128)
    style: Literal['plain','examples_first','concise','deep'] | None=None
    instructions: str | None=Field(default=None,max_length=1000)
    learning_session_id: str | None=None
    regenerate: bool=False

router=APIRouter(route_class=ContractRoute)

@router.get('/api/v1/learning-spaces/{space_id}/nodes/{node_id}/lessons')
def versions(space_id:str,node_id:str,state:LearningStateDep):
    return response(state.lesson_service.list,space_id,node_id)

@router.post('/api/v1/learning-spaces/{space_id}/nodes/{node_id}/lessons',status_code=202)
def create(space_id:str,node_id:str,payload:CreateLesson,state:LearningStateDep,background:BackgroundTasks,key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
    result=response(state.lesson_service.create,space_id,node_id,payload.model_dump(exclude_none=True),key)
    if isinstance(result,dict) and result['status']=='generating':
        row=state.lesson_service.repository.get_record('study_lessons',result['lesson_id'],lock=False)
        background.add_task(ModelTaskWorker(lessons=state.lesson_service).run_once,row['event_id'])
    return result

@router.get('/api/v1/study-lessons/{lesson_id}')
def lesson(lesson_id:str,state:LearningStateDep):return response(state.lesson_service.get,lesson_id)

@router.post('/api/v1/study-lessons/{lesson_id}/retry',status_code=202)
def retry(lesson_id:str,payload:RetryRequest,state:LearningStateDep,background:BackgroundTasks,key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
    result=response(state.lesson_service.retry,lesson_id,key)
    if isinstance(result,dict):
        row=state.lesson_service.repository.get_record('study_lessons',result['lesson_id'],lock=False)
        background.add_task(ModelTaskWorker(lessons=state.lesson_service).run_once,row['event_id'])
    return result
