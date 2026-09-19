"""Origin, authentication, idempotency and request correlation middleware."""

from __future__ import annotations

from uuid import uuid4
import hmac

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..config import LearningSettings
from .contract import request_id_context
from .errors import _error_response


def install_middleware(app: FastAPI, settings: LearningSettings) -> None:
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                       allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "X-Local-Token", "Idempotency-Key", "Last-Event-ID"],
                       expose_headers=["X-Request-ID"], allow_credentials=False)

    @app.middleware("http")
    async def idempotency_guard(request: Request, call_next):
        request_id = str(uuid4())
        token = request_id_context.set(request_id)
        origin = request.headers.get("Origin")
        try:
            if origin is not None and origin not in settings.allowed_origins:
                response = _error_response(403, "ORIGIN_NOT_ALLOWED", "请求来源不在本地服务白名单中")
            elif (settings.local_token and request.method != "OPTIONS" and request.url.path != "/api/v1/health"
                  and not hmac.compare_digest(request.headers.get("X-Local-Token", "").encode("utf-8"), settings.local_token.encode("utf-8"))):
                response = _error_response(401, "LOCAL_TOKEN_REQUIRED", "需要有效的本地会话令牌")
            elif (request.method == "POST" and request.url.path.startswith("/api/v1/")
                  and _requires_idempotency(request.url.path) and not (request.headers.get("Idempotency-Key") or "").strip()):
                response = _error_response(400, "IDEMPOTENCY_KEY_REQUIRED", "此 POST 请求必须提供 Idempotency-Key")
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    response = _error_response(500, "INTERNAL_ERROR", "服务内部错误，请提供 request_id 以便排查")
            response.headers["X-Request-ID"] = request_id
            if origin in settings.allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
                response.headers.add_vary_header("Origin")
            return response
        finally:
            request_id_context.reset(token)


def _requires_idempotency(path: str) -> bool:
    return not (
        path.startswith("/api/v1/health")
        or path.startswith("/api/v1/runs/") and path.endswith("/cancel")
        or path.startswith("/api/v1/assessments/") and path.endswith("/finalize")
        or path.startswith("/api/v1/sessions/") and path.endswith("/finish")
        or path.startswith("/api/v1/sessions/") and path.endswith("/events")
    )
