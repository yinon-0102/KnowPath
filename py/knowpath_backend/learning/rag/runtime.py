"""Explicit opt-in runtime; each request owns its provider state and clients."""
from __future__ import annotations

from threading import Timer, Thread, BoundedSemaphore
import os
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

_REQUEST_SLOTS = BoundedSemaphore(16)


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
    def __init__(self, deadline, spending=None):
        self.deadline = deadline
        self.spending = spending
        self.transport = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        if self.spending is not None:
            self.spending.reserve(request)
        configured = request.extensions.get('timeout', {})
        request.extensions['timeout'] = {key: min(remaining, configured[key])
            if configured.get(key) is not None else remaining
            for key in ('connect', 'read', 'write', 'pool')}
        response = self.transport.handle_request(request)
        if time.monotonic() >= self.deadline:
            response.close()
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        response.stream = DeadlineStream(response.stream, self.deadline)
        return response

    def close(self):
        self.transport.close()


class DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.timer = Timer(max(0, deadline-time.monotonic()), self.stream.close)
        self.timer.daemon = True
        self.timer.start()

    def __iter__(self):
        try:
            for chunk in self.stream:
                if time.monotonic() >= self.deadline:
                    raise VerificationError('RAG_DEADLINE_EXCEEDED')
                yield chunk
            if time.monotonic() >= self.deadline:
                raise VerificationError('RAG_DEADLINE_EXCEEDED')
        finally:
            self.close()

    def close(self):
        self.timer.cancel()
        self.stream.close()


class ConfiguredPipeline:
    def __init__(self, materials, spaces, settings, mode):
        self.materials, self.spaces, self.settings, self.mode = materials, spaces, settings, mode
        self.repo = SqlRagRepository(materials.unit_of_work.engine)

    def answer(self, question, **kwargs):
        timeout = float(os.getenv('RAG_DEADLINE_SECONDS', '120'))
        if not 0 < timeout <= 600:
            raise ValueError('invalid RAG deadline')
        deadline = time.monotonic() + timeout
        if not _REQUEST_SLOTS.acquire(timeout=timeout):
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        outcome = []
        def execute():
            try:
                outcome.append((True, self._answer(question, deadline=deadline, **kwargs)))
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
            raise VerificationError('RAG_DEADLINE_EXCEEDED')
        success, value = outcome[0]
        if not success:
            raise value
        return value

    def _answer(self, question, *, deadline, **kwargs):
        model_budget = model_budget_configuration(self.settings)
        timeout = max(.001, deadline-time.monotonic())
        # One client set per request prevents usage/deadline state leaking
        # between concurrent spaces. Endpoint credentials remain environment-only.
        spending = RequestSpending(spending_configuration())
        with httpx.Client(transport=DeadlineTransport(deadline, spending), follow_redirects=False) as http:
            embedder = embedding_model(self.settings, client=http)
            dense_client = QdrantClient(url=os.getenv('QDRANT_URL', 'http://127.0.0.1:6333'),
                api_key=os.getenv('QDRANT_API_KEY') or None, timeout=timeout, check_compatibility=False,
                transport=DeadlineTransport(deadline))
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
                generator = BudgetedJsonModel(self.settings, client=http, **model_options)
                checker = BudgetedJsonModel(self.settings, client=http, **model_options)
                reranker = DashScopeReranker(self.settings, client=http, max_input_tokens=90000)
                instance = RagPipeline(self.repo, self.materials, self.spaces, plugin, reranker,
                    AnswerVerifier(generator, checker), budget=RetrievalBudget(),
                    timeout_seconds=max(.001, deadline-time.monotonic()), require_b1=self.mode == 'b1')
                result = instance.answer(question, **kwargs)
                result['trace']['spending_budget'] = spending.trace()
                return result
            finally:
                dense_client.close()

    def close(self):
        pass  # All owned clients are closed per request.


def configured_pipeline(materials, spaces, settings=None):
    mode = os.getenv('LEARNING_RAG_PLUGIN', 'legacy').strip().lower()
    if mode not in {'legacy', 'a', 'b1'}:
        raise ValueError('LEARNING_RAG_PLUGIN must be legacy, a, or b1')
    if mode == 'legacy':
        return None
    if not getattr(materials, 'unit_of_work', None):
        raise ValueError('versioned RAG requires SQL storage')
    return ConfiguredPipeline(materials, spaces, settings or LearningSettings.from_env(), mode)
