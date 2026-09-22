"""Explicit opt-in runtime; each request owns its provider state and clients."""
from __future__ import annotations

from threading import Thread, BoundedSemaphore, Event, Lock
from queue import Queue, Empty, Full
import os
import math
import time

import httpx
from qdrant_client import QdrantClient

from ..config import LearningSettings
from ..persistence.rag_repository import SqlRagRepository
from ..providers.models import embedding_model
from .contracts import RetrievalBudget
from .model_services import BudgetedJsonModel
from .pipeline import RagPipeline
from .registry import create_plugin
from .reranking import DashScopeReranker
from .vector import QdrantContentIndex
from .verification import AnswerVerifier, VerificationError
from .spending import RequestSpending, spending_configuration
from .diagnostics import RequestJournal, JournalStream, failure_kind

_REQUEST_SLOTS = BoundedSemaphore(16)
# A stuck provider or close operation keeps its slot until it really exits.
# Never create replacement workers without a slot: Python cannot safely kill
# arbitrary blocked I/O threads. Each request owns at most two transports.
_HTTP_WORKER_SLOTS = BoundedSemaphore(32)


class _Exchange:
    def __init__(self, deadline):
        self.deadline = deadline
        self.events = Queue(maxsize=2)
        self.cancelled = Event()
        self.identifier = None

    def send(self, event):
        while not self.cancelled.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self.events.put(event, timeout=min(.05, remaining))
                return True
            except Full:
                pass
        return False

    def receive(self):
        remaining = self.deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise Empty()
            event = self.events.get(timeout=remaining)
            if time.monotonic() >= self.deadline:
                raise Empty()
        except Empty:
            self.cancelled.set()
            raise VerificationError('RAG_DEADLINE_EXCEEDED') from None
        if event[0] == 'error':
            raise event[1]
        return event


def model_budget_configuration(settings):
    """One explicit input/output reservation shared with frozen evaluation."""
    try:
        context = settings.context_budget_tokens
        if type(context) is not int or context <= 0:
            raise ValueError()
        values = {}
        for key, environment, default in (
            ('model_input_tokens', 'RAG_MODEL_INPUT_TOKENS', '12000'),
            ('model_output_tokens', 'RAG_MODEL_OUTPUT_TOKENS', '2000'),
        ):
            raw = os.getenv(environment, default)
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError()
            value = int(raw)
            if value <= 0:
                raise ValueError()
            values[key] = value
        if sum(values.values()) > context:
            raise ValueError()
        return values
    except (AttributeError, TypeError, ValueError):
        # Configuration errors must not echo accidental secrets or arbitrary
        # environment strings into API or evaluation output.
        raise ValueError('RAG_MODEL_BUDGET_INVALID') from None


class DeadlineTransport(httpx.BaseTransport):
    """Bound every HTTP stage by the remaining request time, without retries."""
    def __init__(self, deadline, spending=None, journal=None):
        self.deadline = deadline
        self.spending = spending
        self.journal = journal
        self._jobs = Queue(maxsize=1)
        self._stop = Event()
        self._start_lock = Lock()
        self._started = False
        self._active = None

    def _start(self, deadline):
        with self._start_lock:
            if self._stop.is_set():
                raise VerificationError('RAG_DEADLINE_EXCEEDED')
            if self._started:
                return
            slots = _HTTP_WORKER_SLOTS
            if not slots.acquire(timeout=max(0, deadline-time.monotonic())):
                raise VerificationError('RAG_DEADLINE_EXCEEDED')
            try:
                worker = Thread(target=self._run, args=(slots,), daemon=True, name='rag-http')
                worker.start()
                self._started = True
            except BaseException:
                slots.release()
                raise

    def _run(self, slots):
        transport = None
        try:
            while not self._stop.is_set() and time.monotonic() < self.deadline:
                try:
                    request, exchange, stage = self._jobs.get(timeout=.05)
                except Empty:
                    continue
                self._active = exchange
                response = None
                try:
                    if self._stop.is_set() or exchange.cancelled.is_set() or time.monotonic() >= exchange.deadline:
                        continue
                    if transport is None:
                        transport = httpx.HTTPTransport(retries=0)
                    if self._stop.is_set() or exchange.cancelled.is_set() or time.monotonic() >= exchange.deadline:
                        continue
                    if self.spending is not None:
                        self.spending.reserve(request)
                    if self._stop.is_set() or exchange.cancelled.is_set() or time.monotonic() >= exchange.deadline:
                        continue
                    if self.journal:
                        exchange.identifier = self.journal.begin_call(stage, billed=stage != 'retrieval')
                    response = transport.handle_request(request)
                    if exchange.cancelled.is_set() or time.monotonic() >= exchange.deadline:
                        continue
                    if response.is_stream_consumed:
                        exchange.send(('response', response))
                        continue
                    if not exchange.send(('headers', response.status_code, response.headers, response.extensions)):
                        continue
                    for chunk in response.stream:
                        if not exchange.send(('chunk', chunk)):
                            break
                    else:
                        exchange.send(('end',))
                except Exception as error:
                    exchange.send(('error', error))
                finally:
                    # Only this owner touches the actual response/transport.
                    # Slow close cannot delay the caller or create more threads.
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                    self._active = None
        finally:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass
            finally:
                slots.release()

    def handle_request(self, request):
        stage_deadline = request.extensions.get('rag_stage_deadline', self.deadline)
        if type(stage_deadline) not in (int, float) or not math.isfinite(stage_deadline):
            raise VerificationError('RAG_DEADLINE_INVALID')
        deadline = min(self.deadline, stage_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        configured = request.extensions.get('timeout', {})
        request.extensions['timeout'] = {key: min(remaining, configured[key])
            if configured.get(key) is not None else remaining
            for key in ('connect', 'read', 'write', 'pool')}
        self._start(deadline)
        exchange = _Exchange(deadline)
        stage = 'retrieval'
        if self.journal:
            path = request.url.path
            stage = ('embedding' if path.endswith('/embeddings') else 'reranking' if path.endswith('/text-rerank')
                     else self.journal.snapshot()['stage'] if path.endswith('/chat/completions') else 'retrieval')
        try:
            self._jobs.put((request, exchange, stage), timeout=max(0, deadline-time.monotonic()))
            event = exchange.receive()
        except Full:
            exchange.cancelled.set()
            raise VerificationError('RAG_DEADLINE_EXCEEDED') from None
        except Exception as error:
            exchange.cancelled.set()
            if exchange.identifier is not None:
                expired = isinstance(error, VerificationError) and error.code == 'RAG_DEADLINE_EXCEEDED'
                self.journal.finish_call(exchange.identifier, status='unknown' if expired else 'failed',
                    failure='deadline' if expired else failure_kind(error))
            raise
        identifier = exchange.identifier
        response = (event[1] if event[0] == 'response' else httpx.Response(
            event[1], headers=event[2], extensions=event[3], stream=DeadlineStream(exchange)))
        if response.is_stream_consumed:
            # Some injected transports return an already buffered response.
            # It will never iterate our stream wrapper through Client.read().
            if identifier is not None:
                from .diagnostics import safe_usage
                try:
                    payload = response.json()
                    usage = safe_usage(payload.get('usage')) if isinstance(payload, dict) else {}
                except (ValueError, UnicodeError):
                    usage = {}
                self.journal.finish_call(identifier, status='failed' if response.status_code >= 400 else 'succeeded',
                    usage=usage, http_status=response.status_code,
                    failure=failure_kind(status=response.status_code) if response.status_code >= 400 else None)
            return response
        if identifier is not None:
            response.stream = JournalStream(response.stream, self.journal, identifier, response.status_code,
                                            response.headers.get('content-encoding', 'identity'))
        return response

    def close(self):
        self._stop.set()
        active = self._active
        if active is not None:
            active.cancelled.set()


class DeadlineStream(httpx.SyncByteStream):
    def __init__(self, exchange):
        self.exchange = exchange

    def __iter__(self):
        try:
            while True:
                event = self.exchange.receive()
                if event[0] == 'end':
                    return
                yield event[1]
        finally:
            self.close()

    def close(self):
        self.exchange.cancelled.set()


class ConfiguredPipeline:
    def __init__(self, materials, spaces, settings, mode):
        self.materials, self.spaces, self.settings, self.mode = materials, spaces, settings, mode
        self.repo = SqlRagRepository(materials.unit_of_work.engine)

    def answer(self, question, **kwargs):
        timeout = float(os.getenv('RAG_DEADLINE_SECONDS', '120'))
        if not 0 < timeout <= 600:
            raise ValueError('invalid RAG deadline')
        deadline = time.monotonic() + timeout
        journal = RequestJournal(deadline)
        if not _REQUEST_SLOTS.acquire(timeout=timeout):
            error = VerificationError('RAG_DEADLINE_EXCEEDED')
            error.call_journal = journal.seal('deadline')
            raise error
        outcome = []
        def execute():
            try:
                journal.set_stage('initialization')
                outcome.append((True, self._answer(question, deadline=deadline, journal=journal, **kwargs)))
            except Exception as error:
                outcome.append((False, error))
            finally:
                _REQUEST_SLOTS.release()
        worker = Thread(target=execute, daemon=True, name='rag-request')
        worker.start()
        worker.join(max(0, deadline-time.monotonic()))
        if worker.is_alive() or time.monotonic() >= deadline:
            # A remote provider can finish an already submitted request later.
            # Its output has no path to message publication; no retry is issued.
            error = VerificationError('RAG_DEADLINE_EXCEEDED', details={'stage': journal.snapshot()['stage']})
            error.call_journal = journal.seal('deadline')
            raise error
        success, value = outcome[0]
        if not success:
            details = value.details if isinstance(value, VerificationError) else {}
            if 'stage' in details:
                journal.set_stage(details['stage'])
            default_failure = {'RAG_DEADLINE_EXCEEDED':'deadline',
                'RAG_STAGE_BUDGET_EXCEEDED':'stage_budget'}.get(getattr(value, 'code', None), 'internal_error')
            journal.fail_phase(details.get('failure_kind', default_failure))
            value.call_journal = journal.seal('deadline' if getattr(value, 'code', None) == 'RAG_DEADLINE_EXCEEDED' else 'failed')
            raise value
        value.setdefault('trace', {})['call_journal'] = journal.seal('succeeded')
        return value

    def _answer(self, question, *, deadline, journal=None, **kwargs):
        model_budget = model_budget_configuration(self.settings)
        from .protocol_config import protocol_configuration
        protocol = protocol_configuration(self.settings)
        journal = journal or RequestJournal(deadline)
        timeout = max(.001, deadline-time.monotonic())
        # One client set per request prevents usage/deadline state leaking
        # between concurrent spaces. Endpoint credentials remain environment-only.
        spending = RequestSpending(spending_configuration())
        with httpx.Client(transport=DeadlineTransport(deadline, spending, journal), follow_redirects=False) as http:
            embedder = embedding_model(self.settings, client=http)
            dense_client = QdrantClient(url=os.getenv('QDRANT_URL', 'http://127.0.0.1:6333'),
                api_key=os.getenv('QDRANT_API_KEY') or None, timeout=timeout, check_compatibility=False,
                transport=DeadlineTransport(deadline, None, journal))
            try:
                profile = {'provider': self.settings.embedding_provider, 'model': embedder.model_version,
                    'dimension': embedder.dimension, 'endpoint': self.settings.embedding_base_url}
                dense = QdrantContentIndex(dense_client, os.getenv('RAG_COLLECTION_PREFIX', 'knowpath_rag_content'),
                    embedder.dimension, profile)
                # Keep tokenized material request-local: deletion must not leave
                # a cross-request cache in another worker process.
                plugin = create_plugin(self.mode, embedder, dense)
                model_options = dict(max_input_tokens=model_budget['model_input_tokens'],
                                     max_output_tokens=model_budget['model_output_tokens'])
                generator = BudgetedJsonModel(self.settings, client=http, journal=journal, stage='generation', **model_options)
                checker = BudgetedJsonModel(self.settings, client=http, journal=journal, stage='verification', **model_options)
                reranker = DashScopeReranker(self.settings, client=http, max_input_tokens=90000)
                from .capacity import AnswerCapacity
                verifier = AnswerVerifier(generator, checker)
                capacity = AnswerCapacity(verifier, generation_seconds=protocol['revision_generation_seconds'],
                    verification_seconds=protocol['revision_verification_seconds'], spending=spending)
                verifier.capacity = capacity
                verifier.revision_admission = capacity.admit_revision
                instance = RagPipeline(self.repo, self.materials, self.spaces, plugin, reranker,
                    verifier, budget=RetrievalBudget(),
                    timeout_seconds=max(.001, deadline-time.monotonic()), require_b1=self.mode in {'b1', 'b2_r1'})
                result = instance.answer(question, **kwargs)
                result['trace']['spending_budget'] = spending.trace()
                return result
            finally:
                dense_client.close()

    def close(self):
        pass  # All owned clients are closed per request.


def configured_pipeline(materials, spaces, settings=None):
    mode = os.getenv('LEARNING_RAG_PLUGIN', 'legacy').strip().lower()
    if mode not in {'legacy', 'a', 'b1', 'b2_r1'}:
        raise ValueError('LEARNING_RAG_PLUGIN must be legacy, a, b1, or b2_r1')
    if mode == 'legacy':
        return None
    if not getattr(materials, 'unit_of_work', None):
        raise ValueError('versioned RAG requires SQL storage')
    return ConfiguredPipeline(materials, spaces, settings or LearningSettings.from_env(), mode)
