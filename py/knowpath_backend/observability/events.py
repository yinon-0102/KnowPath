"""Bounded metadata and context; never serialize business objects or exception text."""
from contextlib import contextmanager
from contextvars import ContextVar, Context
from functools import wraps
import inspect
import logging
import math
import os
import re
from time import perf_counter
from asyncio import CancelledError
from uuid import uuid4

_context = ContextVar('knowpath_log_context', default=None)
http_outcome = ContextVar('knowpath_http_outcome', default=None)
_sensitive = re.compile(r'authorization|cookie|password|passwd|secret|api.?key|access.?token|local.?token|lease.?token|idempotency|prompt|source.?text|answer|rubric|request.?body|response.?body|headers|messages|content', re.I)
_credential = re.compile(
    r'''(?ix)((?:password|passwd|secret|api[_-]?key|access[_-]?token|token)["']?\s*[=:]\s*)'''
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;\'"&}]+)''')
_bearer = re.compile(r'(?i)\bBearer\s+[^\s,;\'"<]+')
_userinfo = re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@')


def redact(text):
    text = str(text)
    for key, value in os.environ.items():
        if value and len(value) >= 4 and re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)', key, re.I):
            text = text.replace(value, '[REDACTED]')
    text = _bearer.sub('Bearer [REDACTED]', text)
    text = _userinfo.sub(r'\1[REDACTED]@', text)
    text = _credential.sub(r'\1[REDACTED]', text)
    return text[:1024] + ('…' if len(text) > 1024 else '')


def safe_value(value, *, depth=0):
    if depth > 4:
        return '[TRUNCATED]'
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if not isinstance(value, float) or math.isfinite(value) else None
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key)[:80]: '[REDACTED]' if _sensitive.search(str(key)) and not (
                    key in {'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens', 'total_tokens'}
                    and type(item) is int and item >= 0) else safe_value(item, depth=depth+1)
                for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [safe_value(item, depth=depth+1) for item in value[:24]]
    return '<' + type(value).__name__ + '>'


def context_fields():
    return dict(_context.get() or {})


def origin_fields():
    """Persist only correlation, never the remaining request or business context."""
    request_id = context_fields().get('request_id')
    return {'request_id': request_id} if request_id else {}


def run_with_log_context(fields, function, *args, **kwargs):
    """Run in an empty Context, importing logging metadata only (never SQL UOWs)."""
    def invoke():
        with bind_context(**fields):
            return function(*args, **kwargs)
    return Context().run(invoke)


@contextmanager
def bind_context(**fields):
    token = _context.set({**context_fields(), **safe_value(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def safe_exception(exc, *, depth=0):
    frames, tb = [], exc.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename.replace('\\', '/')
        filename = 'knowpath_backend/' + filename.split('/knowpath_backend/', 1)[1] if '/knowpath_backend/' in filename else filename.rsplit('/', 1)[-1]
        frames.append({'file': filename[-240:], 'line': tb.tb_lineno, 'function': tb.tb_frame.f_code.co_name[:100]})
        tb = tb.tb_next
    result = {'type': type(exc).__name__, 'frames': frames[-12:]}
    if depth < 2:
        cause = exc.__cause__ or (exc.__context__ if not exc.__suppress_context__ else None)
        if cause is not None and cause is not exc:
            result['cause'] = safe_exception(cause, depth=depth+1)
        if isinstance(exc, BaseExceptionGroup):
            result['children'] = [safe_exception(child, depth=depth+1) for child in exc.exceptions[:3]]
    return result


def record_http_error(code, exc=None):
    outcome = http_outcome.get()
    if outcome is not None:
        outcome['error_code'] = code
        if exc is not None:
            outcome['exception'] = safe_exception(exc)


def log_event(logger, event, *, level=logging.INFO, exc=None, **fields):
    """Fixed event names and explicit metadata; logging cannot fail business work."""
    if not logger.isEnabledFor(level):
        return
    try:
        metadata = {**context_fields(), **fields}
        if exc is not None:
            metadata['exception'] = safe_exception(exc)
        logger.log(level, event, extra={'event': event, 'fields': safe_value(metadata)})
    except Exception:
        # A broken disk, stream, or third-party handler must not mask a domain result.
        return


@contextmanager
def span(logger, event, **fields):
    parent = context_fields().get('span_id')
    with bind_context(span_id=uuid4().hex, parent_span_id=parent):
        with _timed_span(logger, event, **fields) as details:
            yield details


@contextmanager
def _timed_span(logger, event, **fields):
    started = perf_counter()
    details = dict(fields)
    log_event(logger, event + '.started', level=logging.DEBUG, **details)
    try:
        yield details
    except BaseException as exc:
        code = getattr(exc, 'code', None)
        cancelled = isinstance(exc, (GeneratorExit, CancelledError, KeyboardInterrupt))
        expected = type(exc).__name__ in {'DomainConflict', 'DomainNotFound', 'LeaseLost'}
        log_event(logger, event + ('.cancelled' if cancelled else '.failed'),
                  level=logging.INFO if cancelled else logging.WARNING if expected else logging.ERROR, exc=exc,
                  **details, duration_ms=round((perf_counter()-started)*1000, 2), error_code=code)
        raise
    else:
        log_event(logger, event + '.completed', **details, duration_ms=round((perf_counter()-started)*1000, 2))


def observed(event, *, ids=()):
    """Observe synchronous service methods without logging their arguments/results."""
    def decorate(function):
        signature = inspect.signature(function)
        logger = logging.getLogger(function.__module__)
        @wraps(function)
        def call(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            fields = {key: arguments[key] for key in ids if key in arguments}
            with bind_context(**fields), span(logger, event):
                return function(*args, **kwargs)
        return call
    return decorate


def job_observed(function):
    """Record an attempt and its post-execution status, never assume returned=success."""
    logger = logging.getLogger(function.__module__)
    signature = inspect.signature(function)
    event_parameter = tuple(signature.parameters)[1]
    @wraps(function)
    def call(self, *args, **kwargs):
        event = signature.bind(self, *args, **kwargs).arguments[event_parameter]
        payload = event.get('payload', {})
        fields = {'job_id': event['id'], 'run_id': payload.get('run_id'), 'job_type': event.get('event_type'),
                  'attempt': event.get('attempts'), 'resource_id': event.get('aggregate_id')}
        fields.update({key: payload[key] for key in ('space_id','material_id','request_id') if key in payload})
        started = perf_counter()
        with bind_context(**fields):
            log_event(logger, 'worker.attempt.started')
            try:
                result = function(self, *args, **kwargs)
            except BaseException as exc:
                log_event(logger, 'worker.attempt.failed', level=logging.ERROR, exc=exc,
                          duration_ms=round((perf_counter()-started)*1000, 2))
                raise
            status, error_code = 'unavailable', None
            try:
                current = self.repository.get_record('outbox', event['id'], lock=False)
                status = current['status'] if current.get('attempts') == event.get('attempts') else 'superseded'
                if status in {'pending', 'failed', 'processing'}:
                    error_code = (current.get('payload', {}).get('last_error') or {}).get('code')
            except Exception as exc:
                log_event(logger, 'worker.status.unavailable', level=logging.WARNING, exc=exc)
            log_event(logger, 'worker.attempt.finished',
                      level=logging.ERROR if status == 'failed' else logging.WARNING if status in {'pending','processing','superseded','unavailable'} else logging.INFO,
                      observed_status=status, error_code=error_code,
                      duration_ms=round((perf_counter()-started)*1000, 2))
            return result
    return call
