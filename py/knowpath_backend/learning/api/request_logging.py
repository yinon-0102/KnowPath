"""ASGI lifetime logging includes streaming bodies, cancellation and background work."""
import asyncio
import logging
from time import perf_counter
from uuid import uuid4
from starlette.requests import ClientDisconnect

from knowpath_backend.observability import bind_context, log_event, safe_exception
from knowpath_backend.observability.events import http_outcome
from .contract import request_id_context

logger = logging.getLogger('knowpath_backend.http')


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id, started = str(uuid4()), perf_counter()
        request_token = request_id_context.set(request_id)
        outcome = {}
        outcome_token = http_outcome.set(outcome)
        status, size, complete, disconnected, streaming = None, 0, False, False, False
        async def receive_logged():
            nonlocal disconnected
            message = await receive()
            if message['type']=='http.disconnect' and not complete:
                disconnected = True
            return message
        async def send_logged(message):
            nonlocal status, size, complete, streaming, disconnected
            if message['type']=='http.response.start':
                headers = list(message.get('headers', []))
                if not any(key.lower()==b'x-request-id' for key, _ in headers):
                    headers.append((b'x-request-id',request_id.encode('ascii')))
                streaming = any(key.lower()==b'content-type' and b'text/event-stream' in value for key,value in headers)
                message = {**message, 'headers':headers}
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise
            if message['type']=='http.response.start':
                status = message['status']
            elif message['type']=='http.response.body':
                size += len(message.get('body',b''))
                complete = not message.get('more_body',False)
        with bind_context(request_id=request_id):
            try:
                await self.app(scope, receive_logged, send_logged)
            except (asyncio.CancelledError, ClientDisconnect):
                disconnected = True
                raise
            except Exception as exc:
                if not disconnected:
                    outcome.setdefault('exception',safe_exception(exc))
                raise
            finally:
                route = getattr(scope.get('route'), 'path', '<unmatched>')
                failed = (status is not None and status >= 500) or 'exception' in outcome or (not complete and not disconnected)
                quiet = status is not None and status < 400 and (route=='/api/v1/health' or (scope['method']=='GET' and route.startswith('/api/v1/runs/')))
                event = 'http.request.failed' if failed else 'http.request.disconnected' if disconnected else 'http.request.completed'
                log_event(logger, event, level=logging.ERROR if failed else logging.WARNING if (status or 0) >= 400 or disconnected else logging.DEBUG if quiet else logging.INFO,
                    method=scope['method'], route=route, status_code=status, duration_ms=round((perf_counter()-started)*1000,2),
                    response_bytes=size, streaming=streaming, response_complete=complete, **outcome)
                http_outcome.reset(outcome_token)
                request_id_context.reset(request_token)
