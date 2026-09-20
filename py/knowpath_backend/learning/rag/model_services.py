"""One-request budgeted JSON generation for the RAG verification pipeline."""
from __future__ import annotations

import json
import math
import os
import time

import httpx

from knowpath_backend.learning.config import LearningSettings
from .verification import VerificationError


def _remaining_timeout(deadline, maximum, error_type):
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0):
        raise error_type("RAG_DEADLINE_INVALID")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise error_type("RAG_DEADLINE_EXCEEDED")
    return min(remaining, maximum)


def _encoded_size(body):
    """Conservative UTF-8 byte token bound, including the complete JSON envelope."""
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _strict_json(text):
    def constant(_):
        raise ValueError("nonfinite JSON constant")
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("nonfinite JSON number")
        return parsed
    return json.loads(text, parse_constant=constant, object_pairs_hook=object_pairs, parse_float=finite_float)


def _usage_fields(body):
    """Only allowlisted counters, never provider messages, request IDs, or prompts."""
    usage = body.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("invalid usage")
    result = {}
    for name in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"):
        if name in usage:
            value = usage[name]
            if type(value) is not int or value < 0:
                raise ValueError("invalid usage counter")
            result[name] = value
    return result


class BudgetedJsonModel:
    """Synchronous OpenAI-compatible JSON call with no retries or text repair.

    max_input_tokens limits the whole serialized request independently; the
    settings context budget also reserves max_output_tokens before any request.
    last_usage contains counters only and is reset at the start of every call.
    """

    requires_required_points = True

    def __init__(self, settings=None, *, client=None, max_input_tokens=12000, max_output_tokens=2000):
        if any(type(value) is not int or value <= 0 for value in (max_input_tokens, max_output_tokens)):
            raise ValueError("model token budgets must be positive integers")
        self.settings = settings or LearningSettings.from_env()
        self.client = client
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.last_usage = None

    def generate_json(self, messages, *, deadline):
        self.last_usage = None
        started = time.monotonic()
        timeout = _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
        if (not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or not isinstance(m.get("role"), str)
                or m["role"] not in {"system", "user", "assistant"}
                or not isinstance(m.get("content"), str) for m in messages)):
            raise VerificationError("MODEL_INPUT_INVALID")
        body = {"model": self.settings.chat_model, "messages": messages,
                "max_tokens": self.max_output_tokens, "response_format": {"type": "json_object"},
                "stream": False}
        if self.settings.chat_provider == "dashscope" and self.settings.chat_model.lower().startswith("qwen"):
            body["enable_thinking"] = False
        try:
            input_bound = _encoded_size(body)
        except (ValueError, TypeError, UnicodeError):
            raise VerificationError("MODEL_INPUT_INVALID") from None
        if (input_bound > self.max_input_tokens
                or input_bound + self.max_output_tokens > self.settings.context_budget_tokens):
            raise VerificationError("MODEL_TOKEN_BUDGET_EXCEEDED", details={
                'failure_kind':'input_budget' if input_bound > self.max_input_tokens else 'context_budget',
                'input_bound':input_bound, 'input_limit':self.max_input_tokens,
                'output_limit':self.max_output_tokens, 'context_limit':self.settings.context_budget_tokens})
        key = os.getenv(self.settings.chat_api_key_env, "").strip()
        if not key:
            raise VerificationError("MODEL_UNAVAILABLE")
        owned = self.client is None
        client = self.client if self.client is not None else httpx.Client(follow_redirects=False)
        try:
            try:
                timeout = _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
                response = client.post(self.settings.chat_base_url.rstrip("/") + "/chat/completions",
                    json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout, follow_redirects=False)
                response.raise_for_status()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError):
                raise VerificationError("MODEL_UNAVAILABLE") from None
            _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
            details = {'input_bound':input_bound, 'input_limit':self.max_input_tokens,
                'output_limit':self.max_output_tokens, 'context_limit':self.settings.context_budget_tokens}
            failure_kind = 'response_json'
            try:
                payload = _strict_json(response.text)
                # Collect only fully validated counters for diagnostics even
                # when truncation/invalid content prevents a valid response.
                # Keep actual usage validation at its original decision point.
                try:
                    details.update(_usage_fields(payload))
                except (ValueError, TypeError, AttributeError):
                    pass
                failure_kind = 'response_shape'
                choices = payload["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("one completed choice required")
                choice = choices[0]
                message = choice["message"]
                details['finish_reason'] = choice['finish_reason']
                if choice['finish_reason'] != 'stop':
                    failure_kind = 'finish_reason'
                    raise ValueError('incomplete response')
                if message.get('refusal'):
                    failure_kind = 'response_refused'
                    raise ValueError('refused response')
                if (message.get("role") != "assistant" or not isinstance(message.get("content"), str)):
                    raise ValueError("incomplete or refused response")
                failure_kind = 'content_json'
                result = _strict_json(message["content"])
                if not isinstance(result, dict):
                    failure_kind = 'content_shape'
                    raise ValueError("JSON object required")
                failure_kind = 'usage_invalid'
                usage = _usage_fields(payload)
                if usage.get("completion_tokens", usage.get("output_tokens", 0)) > self.max_output_tokens:
                    failure_kind = 'output_budget'
                    raise ValueError("output budget exceeded")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError):
                raise VerificationError("MODEL_INVALID_RESPONSE", details={**details,
                    'failure_kind':failure_kind}) from None
            _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
            self.last_usage = {"model": self.settings.chat_model, "estimated_input_tokens": input_bound,
                               "reserved_output_tokens": self.max_output_tokens,
                               "latency_seconds": time.monotonic() - started, **usage}
            return result
        finally:
            if owned:
                client.close()
