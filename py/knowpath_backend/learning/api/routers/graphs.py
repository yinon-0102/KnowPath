"""HTTP routes for graphs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Header, Query
from fastapi.responses import JSONResponse

from ...errors import DomainConflict, DomainNotFound
from ...graph_schemas import IngestMaterial, ReconcileGraph, PublishGraph
from ..contract import ContractRoute
from ..dependencies import LearningStateDep
from ..errors import _domain_error


router = APIRouter(route_class=ContractRoute)


@router.get("/api/v1/materials/{material_id}/topics")
def material_topics(state: LearningStateDep, material_id: str, version_id: str | None = None,
                    include_inactive: bool = False, depth: int | None = Query(None, ge=1, le=100)) -> JSONResponse:
    try:
        return JSONResponse(content=state.graph_queries.material_topics(material_id, version_id,
                            include_inactive=include_inactive, depth=depth))
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.get("/api/v1/topics/{topic_id}/graph")
def topic_graph(state: LearningStateDep, topic_id: str, depth: int = Query(1, ge=1, le=3), include_sources: bool = True) -> JSONResponse:
    try:
        return JSONResponse(content=state.get_topic_graph(topic_id, depth=depth, include_sources=include_sources))
    except DomainNotFound as exc:
        return _domain_error(exc)


@router.post("/api/v1/materials/{material_id}/reconcile", status_code=202)
def reconcile_material(state: LearningStateDep, material_id: str, payload: ReconcileGraph,
                       idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.graph_service.reconcile(
            material_id, payload.model_dump(mode="json"), idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.get("/api/v1/materials/{material_id}/graph-diff")
def material_graph_diff(state: LearningStateDep, material_id: str, revision_id: str | None = None, include_unchanged: bool = False) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.graph_service.diff(material_id, revision_id, include_unchanged))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/materials/{material_id}/graph-revisions/{revision_id}/publish")
def publish_graph(state: LearningStateDep, material_id: str, revision_id: str, payload: PublishGraph,
                  idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=state.graph_service.publish(
            material_id, revision_id, payload.model_dump(mode="json"), idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)


@router.post("/api/v1/materials/{material_id}/ingest", status_code=202)
def ingest_material(state: LearningStateDep, material_id: str, payload: IngestMaterial,
                    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    try:
        return JSONResponse(status_code=202, content=state.graph_service.ingest(
            material_id, payload.model_dump(), idempotency_key))
    except (DomainNotFound, DomainConflict) as exc:
        return _domain_error(exc)
