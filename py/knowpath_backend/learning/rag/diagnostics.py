"""Bounded request-local metadata, independent of successful answer publication."""
from copy import deepcopy
import json
import math
from threading import RLock
import time
import zlib

import httpx

from .schema_diagnostics import SCHEMA_ERROR_TYPES, SCHEMA_ERROR_PATHS, SCHEMA_SUBCATEGORIES

STAGES = {'queue', 'initialization', 'retrieval', 'embedding', 'reranking', 'generation', 'verification', 'navigation'}
FAILURES = {'connect_timeout', 'read_timeout', 'write_timeout', 'pool_timeout', 'network_error',
    'authentication', 'rate_limited', 'server_error', 'http_error', 'deadline', 'stage_budget', 'internal_error',
    'input_budget', 'context_budget', 'output_budget', 'finish_reason', 'response_json',
    'response_shape', 'response_refused', 'content_json', 'content_shape', 'usage_invalid', 'response_schema'}
USAGE_KEYS = {'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens', 'total_tokens'}


def safe_metadata(value):
    if not isinstance(value, dict): return {}
    output = {}
    for key in ('input_bound', 'input_limit', 'output_limit', 'context_limit', 'draft_limit_bytes'):
        if type(value.get(key)) is int and value[key] >= 0: output[key] = value[key]
    for key, allowed in {
        'count_kind': {'conservative_upper_bound', 'tokenizer_estimate'},
        'count_unit': {'utf8_bytes_plus_token_reserve', 'tokens_plus_envelope_reserve'},
        'finish_reason': {'stop', 'length', 'content_filter', 'tool_calls', 'function_call'},
        'validation': {'content_passed', 'passed', 'failed'},
        'schema_error_type': SCHEMA_ERROR_TYPES,
        'schema_error_path': SCHEMA_ERROR_PATHS,
        'schema_subcategory': SCHEMA_SUBCATEGORIES,
        'schema_rule': {'schema_fields', 'citation_identity', 'citation_relationship', 'required_points',
                        'span_not_found', 'span_ambiguous', 'span_coverage'},
    }.items():
        if type(value.get(key)) is str and value[key] in allowed: output[key] = value[key]
    digest = value.get('count_profile_sha256')
    if isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
        output['count_profile_sha256'] = digest
    return output


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items()
            if key in USAGE_KEYS and type(count) is int and count >= 0}


def failure_kind(error=None, status=None):
    if getattr(error, 'code', None) == 'RAG_DEADLINE_EXCEEDED': return 'deadline'
    if getattr(error, 'code', None) == 'RAG_STAGE_BUDGET_EXCEEDED': return 'stage_budget'
    if status in (401, 403): return 'authentication'
    if status == 429: return 'rate_limited'
    if status is not None and status >= 500: return 'server_error'
    if status is not None and status >= 400: return 'http_error'
    for exception, label in ((httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
            (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout')):
        if isinstance(error, exception): return label
    if isinstance(error, httpx.HTTPError): return 'network_error'
    return 'internal_error'


def safe_journal_snapshot(value):
    """Validate the entire shape; never serialize arbitrary exception attributes."""
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        return {}
    if value.get('stage') not in STAGES or value.get('status') not in {'running', 'succeeded', 'failed', 'deadline'}:
        return {}
    calls = value.get('calls')
    if not isinstance(calls, list) or len(calls) > 128:
        return {}
    clean = []
    for row in calls:
        if (not isinstance(row, dict) or row.get('stage') not in STAGES
                or row.get('status') not in {'running', 'succeeded', 'failed', 'unknown'}
                or type(row.get('call_index')) is not int or row['call_index'] != len(clean) + 1
                or row.get('billing_status') not in {'unknown', 'usage_observed', 'not_applicable'}):
            return {}
        elapsed = row.get('elapsed_ms')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            return {}
        item = {key: row[key] for key in ('call_index', 'stage', 'status', 'billing_status', 'elapsed_ms')}
        item['usage'] = safe_usage(row.get('usage'))
        item.update(safe_metadata(row))
        if row.get('failure_kind') in FAILURES: item['failure_kind'] = row['failure_kind']
        if type(row.get('http_status')) is int and 100 <= row['http_status'] <= 599:
            item['http_status'] = row['http_status']
        clean.append(item)
    result = {'schema_version': 1, 'stage': value['stage'], 'status': value['status'],
              'physical_calls': len(clean), 'calls': clean, 'phase_metadata':safe_metadata(value.get('phase_metadata'))}
    if value.get('failure_kind') in FAILURES: result['failure_kind'] = value['failure_kind']
    return result


class RequestJournal:
    def __init__(self, deadline=None):
        self._lock = RLock()
        self._value = {'schema_version': 1, 'stage': 'queue', 'status': 'running', 'physical_calls': 0, 'calls': []}
        self._starts = {}
        self._deadline = deadline if type(deadline) in (int, float) and math.isfinite(deadline) else None

    def set_stage(self, stage, metadata=None):
        if stage not in STAGES: raise ValueError('invalid diagnostic stage')
        with self._lock:
            if self._value['status'] == 'running':
                if self._value['stage'] != stage or metadata is not None:
                    self._value['phase_metadata'] = safe_metadata(metadata)
                self._value['stage'] = stage

    def annotate_last(self, stage, *, metadata=None, usage=None, failure=None):
        with self._lock:
            if self._value['status'] != 'running': return
            if self._value['calls'] and self._value['calls'][-1]['stage'] == stage:
                row = self._value['calls'][-1]
                row.update(safe_metadata(metadata))
                validated = safe_usage(usage)
                if validated:
                    row['usage'] = validated
                    row['billing_status'] = 'usage_observed'
                if failure in FAILURES: row['failure_kind'] = failure

    def fail_phase(self, kind):
        with self._lock:
            if self._value['status'] == 'running' and kind in FAILURES:
                self._value['failure_kind'] = kind

    def begin_call(self, stage, *, billed=True):
        with self._lock:
            if self._value['status'] != 'running':
                raise RuntimeError('RAG_JOURNAL_CLOSED')
            if len(self._value['calls']) >= 128: raise RuntimeError('RAG_CALL_JOURNAL_LIMIT')
            self.set_stage(stage)
            identifier = len(self._value['calls']) + 1
            self._starts[identifier] = time.monotonic()
            self._value['calls'].append({'call_index': identifier, 'stage': stage, 'status': 'running',
                'usage': {}, 'elapsed_ms': 0, 'billing_status': 'unknown' if billed else 'not_applicable',
                **safe_metadata(self._value.get('phase_metadata'))})
            self._value['physical_calls'] = identifier
            return identifier

    def finish_call(self, identifier, *, status, usage=None, http_status=None, failure=None):
        with self._lock:
            if self._value['status'] != 'running': return
            row = self._value['calls'][identifier - 1]
            if row['status'] != 'running': return
            row.update(status=status, usage=safe_usage(usage),
                       elapsed_ms=round((time.monotonic() - self._starts[identifier]) * 1000, 3))
            if row['usage']: row['billing_status'] = 'usage_observed'
            if http_status is not None: row['http_status'] = http_status
            if failure in FAILURES: row['failure_kind'] = failure

    def snapshot(self):
        with self._lock:
            return safe_journal_snapshot(deepcopy(self._value))

    def seal(self, status):
        with self._lock:
            if self._value['status'] == 'running':
                for row in self._value['calls']:
                    if row['status'] == 'running':
                        # The caller may seal at the request deadline while a
                        # daemon transport worker is still blocked.  Record
                        # the bounded request elapsed time, rather than the
                        # worker's eventual wall time after the deadline;
                        # late completion remains represented by status=unknown.
                        end = time.monotonic()
                        if self._deadline is not None:
                            end = min(end, self._deadline)
                        row.update(status='unknown', elapsed_ms=round(
                            max(0.0, end - self._starts[row['call_index']]) * 1000, 3))
                self._value['status'] = status
                if status == 'deadline': self._value['failure_kind'] = 'deadline'
            return self.snapshot()


class JournalStream(httpx.SyncByteStream):
    """Extract only usage from bounded response bytes; never retain source text."""
    def __init__(self, stream, journal, identifier, status, encoding='identity'):
        self.stream, self.journal, self.identifier, self.status = stream, journal, identifier, status
        self.complete = False
        self.encoding = encoding.lower().strip()

    def __iter__(self):
        body = bytearray()
        oversize = False
        try:
            for chunk in self.stream:
                if not oversize:
                    if len(body) + len(chunk) <= 2_000_000: body.extend(chunk)
                    else: body.clear(); oversize = True
                yield chunk
            usage = {}
            if not oversize:
                try:
                    decoded = body
                    if self.encoding in {'gzip', 'deflate'}:
                        decoder = zlib.decompressobj(31 if self.encoding == 'gzip' else 15)
                        decoded = decoder.decompress(body, 2_000_001)
                        if len(decoded) > 2_000_000 or not decoder.eof:
                            raise ValueError('compressed usage envelope too large')
                    elif self.encoding not in {'identity', ''}:
                        raise ValueError('unsupported usage encoding')
                    payload = json.loads(decoded)
                    usage = safe_usage(payload.get('usage')) if isinstance(payload, dict) else {}
                except (ValueError, UnicodeError, zlib.error): pass
            self.complete = True
            self.journal.finish_call(self.identifier, status='failed' if self.status >= 400 else 'succeeded',
                usage=usage, http_status=self.status,
                failure=failure_kind(status=self.status) if self.status >= 400 else None)
        except Exception as error:
            self.journal.finish_call(self.identifier, status='failed', http_status=self.status,
                                     failure=failure_kind(error))
            raise
        finally:
            body.clear()

    def close(self):
        try:
            self.stream.close()
        finally:
            if not self.complete:
                self.journal.finish_call(self.identifier, status='unknown', http_status=self.status)
