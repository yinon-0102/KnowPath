"""One-request budgeted JSON generation for the RAG verification pipeline."""
from __future__ import annotations

import json
import math
import os
import time
from copy import deepcopy
from hashlib import sha256

import httpx

from knowpath_backend.learning.config import LearningSettings
from .verification import CLAIM_ID_POOL, VerificationError
from .diagnostics import failure_kind as transport_failure_kind
from .token_budget import calibrate_usage
from .protocol_config import counting_profile, protocol_configuration


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


def _identity_bound_schema(messages, response_schema):
    """Bind only known RAG identity fields in a request-local schema copy.

    The capacity planner and physical send both call request_body, so identity
    constraints cannot disappear from token counts or spending reservations.
    An empty planning draft reserves the full bounded claim-ID pool; a real
    draft, including claims=[], narrows it to exactly the generated identities.
    """
    schema = deepcopy(response_schema)
    definitions = schema.get('$defs', {})
    if not ({'WireClaim', 'WireCheck', 'WireRequirement', 'WireEvidenceSpan'} & definitions.keys()):
        return schema
    try:
        payload = _strict_json(messages[-1]['content'])
        evidence = payload['evidence']
        chunk_ids = list(dict.fromkeys(row['chunk_id'] for row in evidence))
        evidence_ids = list(dict.fromkeys(segment['evidence_id'] for row in evidence
                                         for segment in row.get('segments', [])))
        draft = payload.get('draft', {})
        claim_ids = (list(dict.fromkeys(claim['claim_id'] for claim in draft['claims']))
                     if 'claims' in draft else list(CLAIM_ID_POOL))
        if any(type(identifier) is not str or not identifier for identifier in chunk_ids + evidence_ids + claim_ids):
            raise ValueError('invalid identity collection')
        if any(identifier not in CLAIM_ID_POOL for identifier in claim_ids):
            raise ValueError('claim identity outside reserved pool')
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise VerificationError('MODEL_INPUT_INVALID') from None

    def scalar(field, values):
        if values:
            field['enum'] = list(values)
        else:
            field.pop('enum', None)

    def array(field, values):
        scalar(field['items'], values)
        if not values:
            field['maxItems'] = 0
            if 'minItems' in field:
                field['minItems'] = 0

    if 'WireClaim' in definitions:
        fields = definitions['WireClaim']['properties']
        scalar(fields['claim_id'], CLAIM_ID_POOL)
        array(fields['citation_ids'], chunk_ids)
        array(fields['depends_on'], CLAIM_ID_POOL)
    if 'WireCheck' in definitions:
        fields = definitions['WireCheck']['properties']
        scalar(fields['claim_id'], claim_ids)
        array(fields['citation_ids'], chunk_ids)
        if not claim_ids:
            schema['properties']['checks']['maxItems'] = 0
        if not evidence_ids:
            fields['evidence_spans']['maxItems'] = 0
    if 'WireRequirement' in definitions:
        fields = definitions['WireRequirement']['properties']
        array(fields['citation_ids'], chunk_ids)
        array(fields['claim_ids'], claim_ids)
    if 'WireEvidenceSpan' in definitions:
        scalar(definitions['WireEvidenceSpan']['properties']['evidence_id'], evidence_ids)
    return schema


class BudgetedJsonModel:
    """Synchronous OpenAI-compatible JSON call with no retries or text repair.

    max_input_tokens limits the whole serialized request independently; the
    settings context budget also reserves max_output_tokens before any request.
    last_usage contains counters only and is reset at the start of every call.
    """

    requires_required_points = True
    protocol_version = 2
    # A second call is an explicit, budgeted contract correction, not a hidden
    # transport retry. Test doubles and legacy adapters remain fail-closed.
    allows_contract_retry = True

    def __init__(self, settings=None, *, client=None, max_input_tokens=12000, max_output_tokens=2000,
                 journal=None, stage='generation'):
        if any(type(value) is not int or value <= 0 for value in (max_input_tokens, max_output_tokens)):
            raise ValueError("model token budgets must be positive integers")
        self.settings = settings or LearningSettings.from_env()
        self.client = client
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.last_usage = None
        self.journal, self.stage = journal, stage
        protocol = protocol_configuration(self.settings)
        self.max_draft_bytes = protocol['max_draft_bytes']
        self.response_format = protocol['response_format']
        self.verification_reserve_seconds = protocol['revision_verification_seconds'] if journal else 0
        self.counting_profile = counting_profile(self.settings)

    def request_body(self, messages, response_schema=None):
        body = {'model': self.settings.chat_model, 'messages': messages, 'max_tokens': self.max_output_tokens,
                'response_format': {'type': 'json_object'}, 'stream': False}
        if response_schema is not None and self.response_format == 'json_schema':
            body['response_format'] = {'type':'json_schema', 'json_schema': {
                'name':'rag_response', 'strict':True, 'schema':_identity_bound_schema(messages, response_schema)}}
        if self.settings.chat_provider == 'dashscope' and self.settings.chat_model.lower().startswith('qwen'):
            body['enable_thinking'] = False
        return body

    def generate_json(self, messages, *, deadline, response_schema=None):
        previous_calls = self.journal.snapshot()['physical_calls'] if self.journal else 0
        try:
            result = self._generate_json(messages, deadline=deadline, response_schema=response_schema)
        except VerificationError as error:
            if self.journal:
                kind = error.details.get('failure_kind', transport_failure_kind(error))
                self.journal.fail_phase(kind)
                if self.journal.snapshot()['physical_calls'] > previous_calls:
                    self.journal.annotate_last(self.stage, metadata={**error.details, 'validation':'failed'},
                        usage=self.last_usage, failure=kind)
            raise
        if self.journal:
            self.journal.annotate_last(self.stage, metadata={'validation':'content_passed', 'finish_reason':'stop'}, usage=self.last_usage)
        return result

    def _generate_json(self, messages, *, deadline, response_schema=None):
        self.last_usage = None
        if self.journal:
            self.journal.set_stage(self.stage, {})
        started = time.monotonic()
        _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
        if self.stage == 'generation':
            deadline -= self.verification_reserve_seconds
            if deadline <= time.monotonic():
                raise VerificationError('RAG_STAGE_BUDGET_EXCEEDED', details={'stage':'generation'})
        timeout = _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
        if (not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or not isinstance(m.get("role"), str)
                or m["role"] not in {"system", "user", "assistant"}
                or not isinstance(m.get("content"), str) for m in messages)):
            raise VerificationError("MODEL_INPUT_INVALID")
        body = self.request_body(messages, response_schema)
        try:
            counted = self.counting_profile.count_request(body)
            input_bound = counted.value
        except (ValueError, TypeError, UnicodeError):
            raise VerificationError("MODEL_INPUT_INVALID") from None
        if self.journal:
            self.journal.set_stage(self.stage, {'input_bound':input_bound, 'input_limit':self.max_input_tokens,
                'output_limit':self.max_output_tokens, 'context_limit':self.settings.context_budget_tokens,
                'draft_limit_bytes':self.max_draft_bytes, 'count_kind':counted.kind, 'count_unit':counted.unit,
                'count_profile_sha256':sha256(counted.profile_id.encode()).hexdigest()})
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
                    json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout, follow_redirects=False,
                    extensions={'rag_stage_deadline': deadline})
                response.raise_for_status()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError) as error:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                raise VerificationError("MODEL_UNAVAILABLE", details={'failure_kind': transport_failure_kind(error, status)}) from None
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
                    self.last_usage = {'model': self.settings.chat_model, 'estimated_input_tokens': input_bound,
                        'count_kind':counted.kind, 'count_unit':counted.unit, 'count_profile':counted.profile_id,
                        'actual_prompt_tokens':_usage_fields(payload).get('prompt_tokens'),
                        'reserved_output_tokens': self.max_output_tokens,
                        'latency_seconds': time.monotonic() - started, **_usage_fields(payload)}
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
                calibration = calibrate_usage(counted, usage)
                if usage.get("completion_tokens", usage.get("output_tokens", 0)) > self.max_output_tokens:
                    failure_kind = 'output_budget'
                    raise ValueError("output budget exceeded")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError):
                raise VerificationError("MODEL_INVALID_RESPONSE", details={**details,
                    'failure_kind':failure_kind}) from None
            _remaining_timeout(deadline, self.settings.chat_timeout_seconds, VerificationError)
            self.last_usage = {"model": self.settings.chat_model, "estimated_input_tokens": input_bound,
                               'count_kind':counted.kind, 'count_unit':counted.unit, 'count_profile':counted.profile_id,
                               'actual_prompt_tokens':usage.get('prompt_tokens', usage.get('input_tokens')),
                               'input_bound_sufficient':calibration.within_upper_bound,
                               "reserved_output_tokens": self.max_output_tokens,
                               "latency_seconds": time.monotonic() - started, **usage}
            return result
        finally:
            if owned:
                client.close()
