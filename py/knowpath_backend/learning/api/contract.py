"""Strict HTTP validation and safe, correlated error responses."""

from contextvars import ContextVar
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

request_id_context = ContextVar("learning_request_id", default=None)


def error_response(status_code, code, message, details=None):
    return JSONResponse(status_code=status_code, content={"error": {
        "code": code, "message": message, "details": details or {},
        "retryable": status_code >= 500, "request_id": request_id_context.get() or str(uuid4())}})


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        query_fields = {field.alias for field in self.dependant.query_params}
        body_fields = {field.alias for field in self.dependant.body_params}

        async def strict_handler(request):
            unknown = set(request.query_params) - query_fields
            location = "query"
            if not unknown and request.headers.get("content-type", "").split(";")[0].strip() == "multipart/form-data":
                form = await request.form()
                unknown = set(form) - body_fields
                location = "body"
            if unknown:
                raise RequestValidationError([{"type": "extra_forbidden", "loc": (location, field),
                                               "msg": "Extra inputs are not permitted"} for field in sorted(unknown)])
            return await handler(request)
        return strict_handler
