"""HTTP routes for materials."""

from __future__ import annotations

from typing import Annotated, Literal
import asyncio

from fastapi import APIRouter
from fastapi import File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.materials.schemas import UpdateMaterial, DeleteMaterial
from knowpath_backend.learning.materials.service import IdempotencyConflict, MaterialError, MaterialNotFound, UnsupportedMaterial
from knowpath_backend.learning.materials.service import MaterialTooLarge
from ...pagination import page_records
from ..contract import ContractRoute
from ..dependencies import IngestionServiceDep, LearningStateDep, MaterialServiceDep, SourceAccessDep
from ..errors import _domain_error, _error_response


router = APIRouter(route_class=ContractRoute)


@router.post("/api/v1/materials", status_code=201)
async def create_material(ingestion: IngestionServiceDep,
    file: UploadFile = File(...),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    name: str | None = Form(None),
    auto_ingest: bool = Form(True),
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
            content=await file.read(20 * 1024 * 1024 + 1),
            auto_ingest=auto_ingest,
            idempotency_key=idempotency_key,
            name=name,
        )
    except IdempotencyConflict as exc:
        return _error_response(409, "IDEMPOTENCY_CONFLICT", str(exc))
    except MaterialTooLarge:
        return _error_response(413, "MATERIAL_TOO_LARGE", "文件不能超过 20 MiB")
    except UnsupportedMaterial as exc:
        return _error_response(422, "UNSUPPORTED_MATERIAL", str(exc))
    except MaterialError as exc:
        return _error_response(422, "MATERIAL_PARSE_FAILED", str(exc))

    result, run = uploaded.resource, uploaded.run

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
            "run_id": run["id"] if run else None,
            "replayed": result.replayed,
        },
    )


@router.get("/api/v1/materials")
async def list_materials(service: MaterialServiceDep,
    status: Literal["uploaded", "processing", "ready", "needs_review", "failed", "archived"] | None = None,
    cursor: str | None = Query(None, max_length=2048), limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    items = [{"id": material.id, "name": material.name, "type": material.type,
              "status": material.status, "version": material.version,
              "current_version_id": material.current_version_id, "size_bytes": material.size_bytes,
              "created_at": material.created_at.isoformat(), "updated_at": material.updated_at.isoformat()}
             for material in service.repository.list_materials() if status is None or material.status == status]
    try:
        return JSONResponse(content=page_records(items, scope=["materials", status], limit=limit, cursor=cursor))
    except DomainConflict as exc:
        return _domain_error(exc)


@router.post("/api/v1/materials/{material_id}/versions", status_code=201)
async def create_material_version(ingestion: IngestionServiceDep,
    material_id: str,
    file: UploadFile = File(...),
    auto_ingest: bool = Form(True),
    change_note: str | None = Form(None),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    if not idempotency_key or not idempotency_key.strip():
        return _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "资料版本上传必须提供 Idempotency-Key")
    try:
        uploaded = await asyncio.to_thread(ingestion.create_version,
            material_id=material_id,
            change_note=change_note,
            filename=file.filename or "material.txt",
            content=await file.read(20 * 1024 * 1024 + 1),
            auto_ingest=auto_ingest,
            idempotency_key=idempotency_key,
        )
    except MaterialNotFound:
        return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
    except IdempotencyConflict as exc:
        return _error_response(409, "IDEMPOTENCY_CONFLICT", str(exc))
    except MaterialTooLarge:
        return _error_response(413, "MATERIAL_TOO_LARGE", "文件不能超过 20 MiB")
    except UnsupportedMaterial as exc:
        return _error_response(422, "UNSUPPORTED_MATERIAL", str(exc))
    except MaterialError as exc:
        return _error_response(422, "MATERIAL_PARSE_FAILED", str(exc))

    result, run = uploaded.resource, uploaded.run

    return JSONResponse(
        status_code=201,
        content={
            "material_version_id": result.version.id,
            "material_id": result.material.id,
            "status": result.version.status,
            "content_hash": result.version.content_hash,
            "chunk_count": len(result.version.chunks),
            "run_id": run["id"] if run else None,
            "replayed": result.replayed,
        },
    )


@router.get("/api/v1/materials/{material_id}/versions")
async def list_material_versions(service: MaterialServiceDep, state: LearningStateDep, material_id: str, cursor: str | None = Query(None, max_length=2048),
                                limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    if service.repository.get_material(material_id) is None:
        return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
    history = state.graph_service.repository.records("graph_revisions", material_id=material_id)
    published_versions = {}
    for revision in history:
        if revision["status"] in {"published", "superseded"} and revision.get("graph_version") is not None:
            version_id = revision["material_version_id"]
            published_versions[version_id] = max(published_versions.get(version_id, 0), revision["graph_version"])
    items = [{"id": version.id, "material_id": version.material_id, "filename": version.filename,
              "content_hash": version.content_hash, "status": version.status,
              "graph_version": published_versions.get(version.id), "created_at": version.created_at.isoformat()}
             for version in service.repository.list_versions(material_id)]
    try:
        return JSONResponse(content=page_records(items, scope=["material_versions", material_id], limit=limit, cursor=cursor))
    except DomainConflict as exc:
        return _domain_error(exc)


@router.get("/api/v1/materials/{material_id}")
async def get_material(service: MaterialServiceDep, source_access: SourceAccessDep, material_id: str, space_id: str | None = None) -> JSONResponse:
    material = service.repository.get_material(material_id)
    if material is None:
        return _error_response(404, "RESOURCE_NOT_FOUND", "资料不存在", {"material_id": material_id})
    version = service.repository.get_version(material.current_version_id)
    if version and version.chunks:
        try:
            await asyncio.to_thread(source_access.consult, material_id, version.id,
                                    [chunk.id for chunk in version.chunks], space_id)
        except (DomainNotFound, DomainConflict) as exc:
            return _domain_error(exc)
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


@router.get("/api/v1/materials/{material_id}/versions/{version_id}/chunks/{chunk_id}")
async def get_source_chunk(service: MaterialServiceDep, source_access: SourceAccessDep, material_id: str, version_id: str, chunk_id: str, space_id: str | None = None) -> JSONResponse:
    version = service.repository.get_version(version_id)
    if version is None or version.material_id != material_id:
        return _error_response(404, "RESOURCE_NOT_FOUND", "资料版本不存在", {"version_id": version_id})
    chunk = next((item for item in version.chunks if item.id == chunk_id), None)
    if chunk is None:
        return _error_response(404, "RESOURCE_NOT_FOUND", "来源片段不存在", {"chunk_id": chunk_id})
    try:
        await asyncio.to_thread(source_access.consult, material_id, version_id, [chunk_id], space_id)
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
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


@router.patch("/api/v1/materials/{material_id}")
def update_material(service: MaterialServiceDep, material_id: str, payload: UpdateMaterial) -> JSONResponse:
    try:
        material = service.update(material_id, payload.model_dump(exclude_unset=True))
        return JSONResponse(status_code=200, content={
            "id": material.id, "name": material.name, "type": material.type,
            "status": material.status, "version": material.version,
            "current_version_id": material.current_version_id, "size_bytes": material.size_bytes,
            "created_at": material.created_at.isoformat(), "updated_at": material.updated_at.isoformat()})
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.delete("/api/v1/materials/{material_id}", status_code=202)
async def delete_material(state: LearningStateDep, material_id: str, payload: DeleteMaterial) -> JSONResponse:
    try:
        result = await asyncio.to_thread(state.delete_material, material_id, payload.model_dump())
        return JSONResponse(status_code=202, content=result)
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
