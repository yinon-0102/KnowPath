"""HTTP transport for the first learning-domain slice."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, Response

from .config import LearningSettings
from .material_schemas import UpdateMaterial
from .ingestion import MaterialIngestionService
from .repositories import SqlAlchemyMaterialRepository
from .run_repository import SqlAlchemyRunRepository
from .materials import (
    IdempotencyConflict,
    InMemoryMaterialRepository,
    MaterialError,
    MaterialNotFound,
    MaterialService,
    UnsupportedMaterial,
)
from .errors import DomainConflict, DomainNotFound, EventHistoryExpired
from .runs import RunService, stream_run_events
from .state import LearningState
from .space_schemas import CreateSpace, UpdateSpace, SetScope, UpdateProfile, DeleteSpace
from .spaces import topics_for_version
from .assessment_schemas import CreateAssessment, RecordAttempt, FinalizeAssessment, ResetState, GradeReview
from .question_generation import DashScopeQuestionGenerator
from .message_generation import DashScopeAnswerGenerator
from .message_schemas import SendMessage
from .planner_schemas import CreatePlan, UpdateTask, StartSession, SessionEvent, EmptyObject


def create_app(
    service: MaterialService | None = None,
    settings: LearningSettings | None = None,
    *,
    run_service: RunService | None = None,
    question_generator=None,
    answer_generator=None,
) -> FastAPI:
    service = service or MaterialService(InMemoryMaterialRepository())
    settings = settings or LearningSettings.from_env()
    if run_service is None and isinstance(service.repository, SqlAlchemyMaterialRepository):
        uow = service.repository.unit_of_work
        run_service = RunService(SqlAlchemyRunRepository(uow.engine, unit_of_work=uow))
    if run_service is None and isinstance(service.repository, InMemoryMaterialRepository):
        run_service = RunService(service.repository.run_repository)
    state = LearningState(material_repository=service.repository, run_service=run_service,
                          question_generator=question_generator if question_generator is not None else DashScopeQuestionGenerator(settings),
                          answer_generator=answer_generator if answer_generator is not None else DashScopeAnswerGenerator(settings))
    ingestion = MaterialIngestionService(service, state.run_service)
    app = FastAPI(title="Keel Learning", version="0.1.0")
    app.state.learning_state = state

    @app.middleware("http")
    async def idempotency_guard(request: Request, call_next):
        if settings.local_token and request.url.path != "/api/v1/health":
            if request.headers.get("X-Local-Token") != settings.local_token:
                return _error_response(401, "LOCAL_TOKEN_REQUIRED", "需要有效的本地会话令牌")
        if request.method == "POST" and request.url.path.startswith("/api/v1/") and _requires_idempotency(request.url.path):
            if not (request.headers.get("Idempotency-Key") or "").strip():
                return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "此 POST 请求必须提供 Idempotency-Key")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            status_code=422,
            code="INVALID_REQUEST",
            message="请求参数校验失败",
            details={"errors": [{k: v for k, v in error.items() if k != "ctx"} for error in exc.errors()]},
        )

    @app.get("/api/v1/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": "0.1.0",
            "runtime": "keel-learning",
            "dependencies": {
                "mysql": "not_configured",
                "neo4j": "not_configured",
                "vector_store": "qdrant",
                "llm": {"provider": settings.chat_provider, "model": settings.chat_model, "status": "configured"},
                "embedding": {
                    "provider": settings.embedding_provider,
                    "model": settings.embedding_model,
                    "dimension": settings.embedding_dimension,
                    "status": "configured",
                },
            },
        }

    @app.post("/api/v1/materials", status_code=201)
    async def create_material(
        file: UploadFile = File(...),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        name: str | None = Form(None),
    ) -> JSONResponse:
        if not idempotency_key or not idempotency_key.strip():
            return _error_response(
                status_code=400,
                code="IDEMPOTENCY_KEY_REQUIRED",
                message="资料上传必须提供 Idempotency-Key",
            )
        try:
            uploaded = await asyncio.to_thread(ingestion.create,
                filename=file.filename or "material.txt",
                content=await file.read(),
                idempotency_key=idempotency_key,
                name=name,
            )
        except IdempotencyConflict as exc:
            return _error_response(409, "IDEMPOTENCY_CONFLICT", str(exc))
        except UnsupportedMaterial as exc:
            return _error_response(422, "UNSUPPORTED_MATERIAL", str(exc))
        except MaterialError as exc:
            return _error_response(422, "MATERIAL_PARSE_FAILED", str(exc))

        result, run = uploaded.resource, uploaded.run
        state._index_material_topics(result.material.id, result.version)

        return JSONResponse(
            status_code=201,
            content={
                "material": {
                    "id": result.material.id,
                    "name": result.material.name,
                    "type": result.material.type,
                    "status": result.material.status,
                    "version": result.material.version,
                    "current_version_id": result.material.current_version_id,
                    "size_bytes": result.material.size_bytes,
                    "created_at": result.material.created_at.isoformat(),
                    "updated_at": result.material.updated_at.isoformat(),
                },
                "version": {
                    "id": result.version.id,
                    "material_id": result.version.material_id,
                    "filename": result.version.filename,
                    "content_hash": result.version.content_hash,
                    "status": result.version.status,
                    "chunk_count": len(result.version.chunks),
                },
                "run_id": run["id"],
                "replayed": result.replayed,
            },
        )

    @app.get("/api/v1/materials")
    async def list_materials() -> dict:
        return {
            "items": [
                {
                    "id": material.id,
                    "name": material.name,
                    "type": material.type,
                    "status": material.status,
                    "version": material.version,
                    "current_version_id": material.current_version_id,
                    "size_bytes": material.size_bytes,
                    "created_at": material.created_at.isoformat(),
                    "updated_at": material.updated_at.isoformat(),
                }
                for material in service.repository.list_materials()
            ],
            "next_cursor": None,
        }

    @app.post("/api/v1/materials/{material_id}/versions", status_code=201)
    async def create_material_version(
        material_id: str,
        file: UploadFile = File(...),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if not idempotency_key or not idempotency_key.strip():
            return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "资料版本上传必须提供 Idempotency-Key")
        try:
            uploaded = await asyncio.to_thread(ingestion.create_version,
                material_id=material_id,
                filename=file.filename or "material.txt",
                content=await file.read(),
                idempotency_key=idempotency_key,
            )
        except MaterialNotFound:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        except IdempotencyConflict as exc:
            return _error_response(409, "IDEMPOTENCY_CONFLICT", str(exc))
        except UnsupportedMaterial as exc:
            return _error_response(422, "UNSUPPORTED_MATERIAL", str(exc))
        except MaterialError as exc:
            return _error_response(422, "MATERIAL_PARSE_FAILED", str(exc))

        result, run = uploaded.resource, uploaded.run
        state._index_material_topics(result.material.id, result.version)

        return JSONResponse(
            status_code=201,
            content={
                "material_version_id": result.version.id,
                "material_id": result.material.id,
                "status": result.version.status,
                "content_hash": result.version.content_hash,
                "chunk_count": len(result.version.chunks),
                "run_id": run["id"],
                "replayed": result.replayed,
            },
        )

    @app.get("/api/v1/materials/{material_id}/versions")
    async def list_material_versions(material_id: str) -> JSONResponse:
        if service.repository.get_material(material_id) is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        return JSONResponse(
            status_code=200,
            content={
                "items": [
                    {
                        "id": version.id,
                        "material_id": version.material_id,
                        "filename": version.filename,
                        "content_hash": version.content_hash,
                        "status": version.status,
                        "created_at": version.created_at.isoformat(),
                    }
                    for version in service.repository.list_versions(material_id)
                ],
                "next_cursor": None,
            },
        )

    @app.get("/api/v1/materials/{material_id}")
    async def get_material(material_id: str) -> JSONResponse:
        material = service.repository.get_material(material_id)
        if material is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        version = service.repository.get_version(material.current_version_id)
        return JSONResponse(
            status_code=200,
            content={
                "material": {
                    "id": material.id,
                    "name": material.name,
                    "type": material.type,
                    "status": material.status,
                    "version": material.version,
                    "current_version_id": material.current_version_id,
                    "size_bytes": material.size_bytes,
                    "created_at": material.created_at.isoformat(),
                    "updated_at": material.updated_at.isoformat(),
                },
                "version": {
                    "id": version.id,
                    "filename": version.filename,
                    "content_hash": version.content_hash,
                    "status": version.status,
                    "chunks": [
                        {
                            "id": chunk.id,
                            "text": chunk.text,
                            "section_path": list(chunk.section_path),
                            "line_start": chunk.line_start,
                            "line_end": chunk.line_end,
                            "page": chunk.page,
                            "content_hash": chunk.content_hash,
                        }
                        for chunk in (version.chunks if version else [])
                    ],
                }
                if version
                else None,
            },
        )

    @app.get("/api/v1/materials/{material_id}/versions/{version_id}/chunks/{chunk_id}")
    async def get_source_chunk(material_id: str, version_id: str, chunk_id: str) -> JSONResponse:
        version = service.repository.get_version(version_id)
        if version is None or version.material_id != material_id:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料版本不存在", {"version_id": version_id})
        chunk = next((item for item in version.chunks if item.id == chunk_id), None)
        if chunk is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "来源片段不存在", {"chunk_id": chunk_id})
        return JSONResponse(
            status_code=200,
            content={
                "id": chunk.id,
                "material_id": material_id,
                "material_version_id": version_id,
                "text": chunk.text,
                "section_path": list(chunk.section_path),
                "page": chunk.page,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "content_hash": chunk.content_hash,
            },
        )

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        try:
            run = state.get_run(run_id)
            return JSONResponse(status_code=200, content={k: v for k, v in run.items() if k != "events"})
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.get("/api/v1/runs/{run_id}/events", response_model=None,
             responses={200: {"content": {"text/event-stream": {}}}})
    async def get_run_events(run_id: str, request: Request) -> StreamingResponse | JSONResponse:
        cursor = request.headers.get("Last-Event-ID", "0")
        if not re.fullmatch(r"[0-9]{1,20}", cursor):
            return _error_response(422, "INVALID_EVENT_ID", "Last-Event-ID 须为非负整数")
        last_event_id = int(cursor)
        try:
            # Validate before sending HTTP headers so missing/expired history is JSON.
            await asyncio.to_thread(state.events_for, run_id, after_id=last_event_id)
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)
        return StreamingResponse(
            stream_run_events(state.run_service, run_id, after_id=last_event_id,
                              is_disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str) -> JSONResponse:
        try:
            run = state.cancel_run(run_id)
            status_code = 200 if run["status"] in {"succeeded", "failed", "cancelled"} else 202
            return JSONResponse(status_code=status_code, content={"id": run_id, "status": run["status"]})
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.get("/api/v1/materials/{material_id}/topics")
    def material_topics(material_id: str, version_id: str | None = None) -> JSONResponse:
        material = service.repository.get_material(material_id)
        if material is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        version = service.repository.get_version(version_id or material.current_version_id)
        if version is None or version.material_id != material_id:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料版本不存在")
        topics = topics_for_version(material_id, version)
        state.topics.update({topic["id"]: topic for topic in topics})
        return JSONResponse(status_code=200, content={"material_id": material_id, "version_id": version.id if version else None, "items": topics})

    @app.get("/api/v1/topics/{topic_id}/graph")
    def topic_graph(topic_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.get_topic_graph(topic_id))
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.post("/api/v1/materials/{material_id}/reconcile", status_code=202)
    async def reconcile_material(material_id: str) -> JSONResponse:
        if service.repository.get_material(material_id) is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        run = state.run("graph_reconcile", {"type": "material", "id": material_id})
        return JSONResponse(status_code=202, content={"run_id": run["id"], "status": run["status"]})

    @app.get("/api/v1/materials/{material_id}/graph-diff")
    async def material_graph_diff(material_id: str) -> JSONResponse:
        if service.repository.get_material(material_id) is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        return JSONResponse(status_code=200, content={"base_graph_version": 1, "candidate_revision_id": None, "added": [], "changed": [], "removed": [], "conflicts": [], "affected_topic_ids": []})

    @app.post("/api/v1/materials/{material_id}/graph-revisions/{revision_id}/publish")
    async def publish_graph(material_id: str, revision_id: str, payload: dict[str, Any]) -> JSONResponse:
        if service.repository.get_material(material_id) is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        return JSONResponse(status_code=200, content={"graph_version": 2, "material_version_id": revision_id, "published_at": _now_for_api()})

    @app.get("/api/v1/learning-spaces")
    def list_learning_spaces() -> dict[str, Any]:
        return {"items": state.list_spaces(), "next_cursor": None}

    @app.post("/api/v1/learning-spaces", status_code=201)
    def create_learning_space(payload: CreateSpace, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=201, content=state.create_space(payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.patch("/api/v1/learning-spaces/{space_id}")
    def update_learning_space(space_id: str, payload: UpdateSpace, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.update_space(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/learning-spaces/{space_id}")
    def get_learning_space(space_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.get_space(space_id))
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/scope")
    def set_learning_scope(space_id: str, payload: SetScope, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.set_scope(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/learning-spaces/{space_id}/profile")
    def get_learning_profile(space_id: str) -> JSONResponse:
        try:
            space = state.get_space(space_id)
            return JSONResponse(status_code=200, content={"profile": space["profile"], "profile_version": space["profile_version"]})
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.patch("/api/v1/learning-spaces/{space_id}/profile")
    def update_learning_profile(space_id: str, payload: UpdateProfile, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.update_profile(space_id, payload.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/learning-spaces/{space_id}/state")
    def get_learning_state(space_id: str, topic_id: str | None = None, status: str | None = None, include_evidence: bool = False) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.get_state(space_id, topic_id=topic_id, status=status, include_evidence=include_evidence))
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/assessments", status_code=202)
    def create_assessment(space_id: str, payload: CreateAssessment, background_tasks: BackgroundTasks, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            assessment = state.create_assessment(space_id, payload.model_dump(exclude_none=True), idempotency_key=idempotency_key, dispatch=background_tasks.add_task)
            return JSONResponse(status_code=202, content={"run_id": assessment["run_id"], "assessment_id": assessment["id"], "assessment": assessment, "status": assessment["status"]})
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/assessments/{assessment_id}")
    def get_assessment(assessment_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.get_assessment(assessment_id))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/assessments/{assessment_id}/attempts")
    def record_attempt(assessment_id: str, payload: RecordAttempt, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=202 if payload.finalize else 200, content=state.record_attempt(assessment_id, payload.model_dump(), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/assessments/{assessment_id}/finalize", status_code=202)
    def finalize_assessment(assessment_id: str, payload: FinalizeAssessment) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.finalize_assessment(assessment_id, payload.model_dump()))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/assessments/{assessment_id}/result")
    def assessment_result(assessment_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.assessment_result(assessment_id))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/plans", status_code=202)
    async def create_plan(space_id: str, payload: CreatePlan, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            plan = state.create_plan(space_id, payload.model_dump(mode="json", exclude_none=True), idempotency_key=idempotency_key)
            return JSONResponse(status_code=202, content={"run_id": plan["run_id"], "plan_id": plan["plan_id"], "status": plan["status"]})
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/plans/{plan_id}")
    async def get_plan(plan_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.get_plan(plan_id))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.patch("/api/v1/plans/{plan_id}/tasks/{task_id}")
    async def update_plan_task(plan_id: str, task_id: str, payload: UpdateTask) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.update_task(plan_id, task_id, payload.model_dump(mode="json", exclude_none=True)))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/plans/{plan_id}/sessions", status_code=201)
    async def start_learning_session(plan_id: str, payload: StartSession, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=201, content=state.start_session(plan_id, payload.task_id, idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/sessions/{session_id}/events", status_code=201)
    async def add_learning_event(session_id: str, payload: SessionEvent, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        if not (idempotency_key or payload.event_id):
            return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "会话事件必须提供 Idempotency-Key 或 event_id")
        try:
            return JSONResponse(status_code=201, content=state.add_session_event(session_id, payload.model_dump(mode="json", exclude_none=True), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/sessions/{session_id}/finish")
    async def finish_learning_session(session_id: str, _: EmptyObject) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.finish_session(session_id))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/messages", status_code=202)
    def send_learning_message(space_id: str, payload: SendMessage, background_tasks: BackgroundTasks,
                              idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.send_message(space_id, payload.model_dump(), idempotency_key=idempotency_key,
                                                                           dispatch=background_tasks.add_task))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/learning-spaces/{space_id}/evidence")
    def list_evidence(space_id: str, topic_id: str | None = None, kind: str | None = None,
                      from_time: datetime | None = Query(None, alias="from"),
                      to_time: datetime | None = Query(None, alias="to"),
                      limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=2048)) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.assessment_service.evidence_page(space_id,
                topic_id=topic_id, kind=kind, from_time=from_time, to_time=to_time, limit=limit, cursor=cursor))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/knowledge-corrections", status_code=201)
    async def create_knowledge_correction(space_id: str, payload: dict[str, Any]) -> JSONResponse:
        try:
            return JSONResponse(status_code=201, content=state.create_correction(space_id, payload))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/knowledge-corrections/{correction_id}/confirm", status_code=202)
    async def confirm_knowledge_correction(space_id: str, correction_id: str, payload: dict[str, Any]) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.confirm_correction(correction_id, payload))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/learning-spaces/{space_id}/changes")
    async def list_changes(space_id: str) -> JSONResponse:
        try:
            state.get_space(space_id)
            return JSONResponse(status_code=200, content={"items": state.changes_for(space_id), "next_cursor": None})
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.post("/api/v1/materials/{material_id}/ingest", status_code=202)
    async def ingest_material(material_id: str, payload: dict[str, Any]) -> JSONResponse:
        if service.repository.get_material(material_id) is None:
            return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
        run = state.run("material_ingest", {"type": "material", "id": material_id})
        return JSONResponse(status_code=202, content={"run_id": run["id"], "status": "queued"})

    @app.get("/api/v1/learning-spaces/{space_id}/knowledge-updates")
    async def knowledge_updates(space_id: str) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.knowledge_updates(space_id))
        except DomainNotFound as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/knowledge-updates/apply", status_code=202)
    async def apply_knowledge_updates(space_id: str, payload: dict[str, Any]) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.apply_knowledge_updates(space_id, payload))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/state/reset")
    def reset_learning_state(space_id: str, payload: ResetState, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=200, content=state.reset_state(space_id, payload.topic_ids, payload.reason, expected_state_version=payload.expected_state_version, idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/assessments/{assessment_id}/grade-reviews", status_code=202)
    def grade_review(assessment_id: str, payload: GradeReview, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.grade_review(assessment_id, payload.model_dump(), idempotency_key=idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.post("/api/v1/learning-spaces/{space_id}/exports", status_code=202)
    def create_export(space_id: str, payload: dict[str, Any], idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
        try:
            return JSONResponse(status_code=202, content=state.create_export(space_id, payload, idempotency_key))
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.get("/api/v1/exports/{export_id}/download")
    def download_export(export_id: str) -> Response:
        try:
            return Response(content=state.export_archive(export_id), media_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="learning-space.zip"'})
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.patch("/api/v1/materials/{material_id}")
    def update_material(material_id: str, payload: UpdateMaterial) -> JSONResponse:
        try:
            material = service.update(material_id, payload.model_dump(exclude_unset=True))
            return JSONResponse(status_code=200, content={
                "id": material.id, "name": material.name, "type": material.type,
                "status": material.status, "version": material.version,
                "current_version_id": material.current_version_id, "size_bytes": material.size_bytes,
                "created_at": material.created_at.isoformat(), "updated_at": material.updated_at.isoformat()})
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.delete("/api/v1/learning-spaces/{space_id}", status_code=202)
    def delete_learning_space(space_id: str, payload: DeleteSpace) -> JSONResponse:
        try:
            result = state.delete_space(space_id, payload.model_dump())
            return JSONResponse(status_code=202, content=result)
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)

    @app.delete("/api/v1/materials/{material_id}", status_code=202)
    async def delete_material(material_id: str, payload: dict[str, Any] | None = None) -> JSONResponse:
        def delete_atomically():
            with service.repository.transaction():
                material = service.repository.get_material(material_id)
                if material is None:
                    raise DomainNotFound("material", material_id)
                referenced = any(material_id in [binding["material_id"] for binding in space["bindings"]]
                                for space in state.list_spaces())
                if referenced:
                    raise DomainConflict("MATERIAL_IN_USE", "资料仍被学习空间引用；请先删除引用空间，级联删除尚未实现")
                service.repository.delete_material(material_id)
        try:
            await asyncio.to_thread(delete_atomically)
        except DomainNotFound as exc:
            return _domain_error(exc)
        except DomainConflict as exc:
            return _domain_error(exc)
        run = state.run("material_delete", {"type": "material", "id": material_id})
        return JSONResponse(status_code=202, content={"run_id": run["id"], "status": "queued"})
    return app


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "retryable": status_code >= 500,
            }
        },
    )


def _domain_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DomainNotFound):
        return _error_response(404, "RESOURCE_NOT_FOUND", str(exc), {"id": exc.resource_id})
    if isinstance(exc, EventHistoryExpired):
        return _error_response(410, exc.code, str(exc))
    if isinstance(exc, DomainConflict):
        status = 422 if exc.code.startswith("INVALID_") or exc.code.endswith("_REQUIRED") else 409
        if exc.code in {"EXPORT_EXPIRED", "RESOURCE_DELETED"}:
            status = 410
        if exc.code == "PLAN_CONSTRAINT_UNSATISFIABLE":
            status = 422
        return _error_response(status, exc.code, str(exc), exc.details)
    return _error_response(500, "INTERNAL_ERROR", str(exc))


def _now_for_api() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _requires_idempotency(path: str) -> bool:
    return not (
        path.startswith("/api/v1/health")
        or path.startswith("/api/v1/runs/") and path.endswith("/cancel")
        or path.startswith("/api/v1/assessments/") and path.endswith("/finalize")
        or path.startswith("/api/v1/sessions/") and path.endswith("/finish")
        or path.startswith("/api/v1/sessions/") and path.endswith("/events")
    )
