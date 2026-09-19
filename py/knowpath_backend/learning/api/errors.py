"""Translate validation and domain failures to the HTTP error contract."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..config import LearningSettings
from ..errors import DomainConflict, DomainNotFound, EventHistoryExpired
from knowpath_backend.learning.conversations.generation import DashScopeAnswerGenerator
from knowpath_backend.learning.providers.models import chat_model, ModelError
from knowpath_backend.learning.assessments.generation import DashScopeQuestionGenerator
from .contract import error_response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        codes = {404: "RESOURCE_NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return _error_response(exc.status_code, codes.get(exc.status_code, "INVALID_REQUEST"), "请求的资源或方法不可用")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            status_code=422,
            code="INVALID_REQUEST",
            message="请求参数校验失败",
            details={"errors": [{k: v for k, v in error.items() if k not in {"ctx", "input"}} for error in exc.errors()]},
        )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    return error_response(status_code, code, message, details)


def _domain_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DomainNotFound):
        return _error_response(404, "RESOURCE_NOT_FOUND", str(exc), {"id": exc.resource_id})
    if isinstance(exc, EventHistoryExpired):
        return _error_response(410, exc.code, str(exc))
    if isinstance(exc, DomainConflict):
        status = 422 if exc.code.startswith("INVALID_") or exc.code.endswith("_REQUIRED") else 409
        if exc.code in {"EXPORT_EXPIRED", "RESOURCE_DELETED"}:
            status = 410
        if exc.code in {"PLAN_CONSTRAINT_UNSATISFIABLE", "UNSUPPORTED_MODEL"}:
            status = 422
        return _error_response(status, exc.code, str(exc), exc.details)
    return _error_response(500, "INTERNAL_ERROR", str(exc))


def validate_chat(generator, settings: LearningSettings) -> None:
    if isinstance(generator, (DashScopeQuestionGenerator, DashScopeAnswerGenerator)):
        try:
            model = chat_model(settings)
            if "json" not in getattr(model, "capabilities", ()):
                raise ModelError("UNSUPPORTED_MODEL")
        except ModelError as exc:
            raise DomainConflict(exc.code, "配置的模型不支持当前任务所需能力") from None
