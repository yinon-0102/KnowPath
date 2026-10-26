"""Request-local model accounting and a single-attempt Web provider boundary.

Input uses UTF-8 envelope bytes plus a server-template allowance, not measured
provider tokens. Output is reserved at its advertised maximum for every attempt,
including failures. Injected ports must themselves disable physical retries.
The HTTP implementation deliberately buffers a completion before callbacks.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import json
import math
import time

import httpx
from openai.types.chat import ChatCompletion

from knowpath_backend.core.turn import TurnBudgetExceeded, TurnCancelled
from knowpath_backend.learning.providers.models import DashScopeChatAdapter, ModelError


@dataclass(frozen=True)
class WebModelBudget:
    max_calls: int = 5
    max_input_tokens: int = 128000
    max_output_tokens: int = 8192
    output_per_call: int = 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (
                self.max_calls, self.max_input_tokens, self.max_output_tokens, self.output_per_call)):
            raise ValueError('Model budgets must be positive integers')
        if self.output_per_call > self.max_output_tokens:
            raise ValueError('Per-call output exceeds total output budget')


class BudgetedModelPort:
    def __init__(self, port, budget=None, *, should_cancel=None):
        self.port, self.budget = port, budget or WebModelBudget()
        self.should_cancel = should_cancel
        self.calls = self.input_reserved = self.output_reserved = 0

    def complete(self, request, *, stream=False, on_text_chunk=None,
                 on_reasoning_chunk=None, should_cancel=None, strict_callbacks=True):
        def cancelled():
            return bool((self.should_cancel and self.should_cancel()) or
                        (should_cancel and should_cancel()))
        if cancelled():
            raise TurnCancelled('Turn cancelled before model call')
        body = dict(request)
        output = body.get('max_tokens', self.budget.output_per_call)
        if type(output) is not int or output <= 0:
            raise ValueError('Positive output reservation required')
        body['max_tokens'] = output
        count = (len(json.dumps(body, ensure_ascii=False, separators=(',', ':'),
                               allow_nan=False).encode('utf-8')) +
                 64 + 32 * len(body.get('messages', [])))
        if (self.calls >= self.budget.max_calls or
                self.input_reserved + count > self.budget.max_input_tokens or
                self.output_reserved + output > self.budget.max_output_tokens):
            raise TurnBudgetExceeded('Web model budget exhausted')
        # Charge before dispatch: transport failures may already have consumed tokens.
        self.calls += 1
        self.input_reserved += count
        self.output_reserved += output
        texts, reasons = [], []
        response = self.port.complete(body, stream=stream, on_text_chunk=texts.append,
            on_reasoning_chunk=reasons.append, should_cancel=cancelled, strict_callbacks=True)
        if cancelled() or getattr(response, '_cancelled', False):
            raise TurnCancelled('Turn cancelled during model call')
        usage = getattr(response, 'usage', None)
        completion_tokens = getattr(usage, 'completion_tokens', None)
        if completion_tokens is not None and completion_tokens > output:
            raise TurnBudgetExceeded('Provider exceeded output reservation')
        for callback, chunks in ((on_text_chunk, texts), (on_reasoning_chunk, reasons)):
            if callback is not None:
                for chunk in chunks:
                    if cancelled():
                        raise TurnCancelled('Turn cancelled during callback')
                    callback(chunk)
        return response


class OneShotChatPort:
    """DashScope configuration, one HTTP attempt, no SDK defaults or retries."""
    def __init__(self, settings, *, client=None, deadline=None, should_cancel=None):
        if settings.chat_provider != 'dashscope':
            raise ModelError('UNSUPPORTED_MODEL')
        self.settings = settings
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            from knowpath_backend.learning.rag.verification import VerificationError
            raise VerificationError('RAG_DEADLINE_INVALID')
        self.deadline, self.should_cancel = deadline, should_cancel
        self.adapter = DashScopeChatAdapter(settings, client=client)

    @contextmanager
    def _client(self, deadline, cancelled):
        if self.adapter.client is not None:
            yield self.adapter.client
        else:
            # Absolute waiting is bounded even when the provider trickles data
            # or ignores socket inactivity timeouts. The transport owns its
            # globally bounded I/O worker until the physical request exits.
            from knowpath_backend.learning.rag.runtime import DeadlineTransport
            with httpx.Client(transport=DeadlineTransport(deadline, should_cancel=cancelled),
                              follow_redirects=False) as client:
                yield client

    def complete(self, request, *, stream=False, on_text_chunk=None,
                 on_reasoning_chunk=None, should_cancel=None, strict_callbacks=True):
        def cancelled():
            return bool((self.should_cancel and self.should_cancel()) or
                        (should_cancel and should_cancel()))
        deadline = min(self.deadline if self.deadline is not None else float('inf'),
            time.monotonic()+self.settings.chat_timeout_seconds)
        def guard():
            if cancelled():
                raise TurnCancelled('Turn cancelled during model call')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                from knowpath_backend.learning.rag.verification import VerificationError
                raise VerificationError('RAG_DEADLINE_EXCEEDED')
            return remaining
        guard()
        body = dict(request)
        if body.get('model') != self.settings.chat_model:
            raise ModelError('UNSUPPORTED_MODEL')
        body.update(enable_thinking=False, stream=False)
        try:
            with self._client(deadline, cancelled) as client:
                response = client.post(self.settings.chat_base_url.rstrip('/') + '/chat/completions',
                    json=body, headers=self.adapter._headers(),
                    timeout=guard(), follow_redirects=False)
                if response.status_code == 429:
                    raise ModelError('RATE_LIMITED')
                response.raise_for_status()
                result = ChatCompletion.model_validate(response.json())
                if not result.choices:
                    raise ValueError('Missing choices')
        except (httpx.HTTPError, httpx.InvalidURL):
            raise ModelError('MODEL_UNAVAILABLE') from None
        except (ValueError, TypeError, KeyError):
            raise ModelError('MODEL_INVALID_RESPONSE') from None
        guard()
        content = result.choices[0].message.content
        if content and on_text_chunk:
            on_text_chunk(content)
        return result
