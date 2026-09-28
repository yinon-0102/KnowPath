"""Safe physical-action events shared by adapters, journals and tool execution."""
from contextlib import contextmanager
from asyncio import CancelledError
import logging
from threading import RLock
from time import perf_counter
from uuid import uuid4

from .events import bind_context, context_fields, log_event

USAGE_KEYS = ('prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens', 'total_tokens')
FINISH_REASONS = {'stop', 'length', 'content_filter', 'tool_calls', 'function_call', 'end_turn', 'max_tokens', 'tool_use'}


def usage_metrics(value):
    """Never serialize SDK objects or confuse an absent count with zero usage."""
    usage = {}
    for key in USAGE_KEYS:
        count = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if type(count) is int and count >= 0:
            usage[key] = count
    return {'usage': usage, 'usage_status': 'observed' if usage else 'unknown'}


def response_metrics(response):
    value = response if isinstance(response, dict) else {}
    usage = value.get('usage') if value else getattr(response, 'usage', None)
    result = usage_metrics(usage)
    choices = value.get('choices') if value else getattr(response, 'choices', None)
    if isinstance(choices, (list, tuple)) and choices:
        first = choices[0]
        reason = first.get('finish_reason') if isinstance(first, dict) else getattr(first, 'finish_reason', None)
        if isinstance(reason, str) and reason in FINISH_REASONS:
            result['finish_reason'] = reason
    return result


def collect_response_metrics(fields, response):
    """Stream frames without usage must not erase an earlier observed count."""
    metrics = response_metrics(response)
    if metrics['usage_status'] == 'unknown':
        metrics.pop('usage')
        metrics.pop('usage_status')
    fields.update(metrics)


def model_post(logger, client, url, *, provider, model, stage, **kwargs):
    """Observe an HTTP exchange once; a journaled transport owns its own events."""
    with bind_context(provider=provider, model=model, stage=stage):
        if getattr(getattr(client, '_transport', None), 'journal', None) is not None:
            return client.post(url, **kwargs)
        with model_call(logger, provider=provider, model=model, stage=stage, observation_scope='http') as fields:
            response = client.post(url, **kwargs)
            fields['status_code'] = response.status_code
            try:
                fields.update(response_metrics(response.json()))
            except (ValueError, TypeError, AttributeError):
                pass  # Diagnostics do not replace caller validation.
            return response


class Action:
    """An action may finish in another thread; one terminal event at most."""
    def __init__(self, logger, event='model.call', **fields):
        self.logger, self.event = logger, event
        self.started = perf_counter()
        self._lock, self._finished = RLock(), False
        identifier = uuid4().hex
        self.context = {**context_fields(), 'span_id': identifier,
                        'parent_span_id': context_fields().get('span_id')}
        self.fields = {'action_id': identifier, **fields}
        if event == 'model.call':
            self.fields.update(model_call_id=identifier, **usage_metrics(None))
        elif event == 'tool.call':
            self.fields['tool_call_id'] = identifier
        self.record('started')

    def record(self, suffix, *, level=logging.INFO, exc=None, **fields):
        log_event(self.logger, self.event + '.' + suffix, level=level, exc=exc,
                  **{**self.context, **self.fields, **fields})

    def finish(self, status='completed', *, exc=None, **fields):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self.fields.update(fields)
            self.record(status, exc=exc, duration_ms=round((perf_counter()-self.started)*1000, 3),
                        level=logging.ERROR if status == 'failed' else logging.WARNING if status in {'unknown','timed_out','rejected'} else logging.INFO)


@contextmanager
def model_call(logger, **fields):
    action = Action(logger, **fields)
    with bind_context(**{**action.context, 'model_call_id': action.fields['model_call_id']}):
        try:
            yield action.fields
        except BaseException as exc:
            cancelled = isinstance(exc, (CancelledError, GeneratorExit, KeyboardInterrupt))
            action.finish('cancelled' if cancelled else 'failed', exc=exc, error_code=getattr(exc, 'code', None))
            raise
        else:
            status = ('cancelled' if action.fields.get('cancelled') else
                      'failed' if (action.fields.get('status_code') or 0) >= 400 else 'completed')
            action.finish(status)
