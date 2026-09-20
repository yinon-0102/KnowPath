"""Independent DashScope text reranking adapter."""
from __future__ import annotations

import math
import os
import time

import httpx

from knowpath_backend.learning.config import LearningSettings
from .model_services import _encoded_size, _remaining_timeout, _strict_json, _usage_fields
from .retrieval import RetrievalError

DEFAULT_RERANK_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
DEFAULT_RERANK_MODEL = "gte-rerank-v2"


class DashScopeReranker:
    """One bounded rerank request, requiring every input index exactly once.

    The endpoint is independent from the embedding/chat compatible-mode base.
    gte-rerank-v2 is an experiment default, not a claim of task-specific quality.
    """

    def __init__(self, settings=None, *, client=None, endpoint=None, key_env=None, model=None,
                 max_candidates=40, max_input_tokens=12000):
        if any(type(value) is not int or value <= 0 for value in (max_candidates, max_input_tokens)):
            raise ValueError("rerank budgets must be positive integers")
        self.settings = settings or LearningSettings.from_env()
        self.client = client
        self.endpoint = endpoint or os.getenv("RAG_RERANK_BASE_URL") or DEFAULT_RERANK_ENDPOINT
        self.key_env = key_env or os.getenv("RAG_RERANK_API_KEY_ENV") or self.settings.embedding_api_key_env
        self.model = model or os.getenv("RAG_RERANK_MODEL") or DEFAULT_RERANK_MODEL
        self.max_candidates, self.max_input_tokens = max_candidates, max_input_tokens
        self.last_usage = None

    def rerank(self, query, chunks, *, deadline):
        self.last_usage = None
        started = time.monotonic()
        _remaining_timeout(deadline, self.settings.chat_timeout_seconds, RetrievalError)
        if (not isinstance(query, str) or not query.strip() or not isinstance(chunks, list)
                or any(not isinstance(c, dict) or not isinstance(c.get("retrieval_text"), str)
                       or not c["retrieval_text"].strip() for c in chunks)):
            raise RetrievalError("RERANK_INPUT_INVALID")
        if len(chunks) > self.max_candidates:
            raise RetrievalError("RERANK_CANDIDATE_BUDGET_EXCEEDED")
        if not chunks:
            return []
        body = {"model": self.model, "input": {"query": query, "documents": [c["retrieval_text"] for c in chunks]},
                "parameters": {"top_n": len(chunks), "return_documents": False}}
        try:
            input_bound = _encoded_size(body)
        except (ValueError, TypeError, UnicodeError):
            raise RetrievalError("RERANK_INPUT_INVALID") from None
        if input_bound > self.max_input_tokens:
            raise RetrievalError("RERANK_TOKEN_BUDGET_EXCEEDED")
        key = os.getenv(self.key_env, "").strip()
        if not key:
            raise RetrievalError("RERANK_UNAVAILABLE")
        owned = self.client is None
        client = self.client if self.client is not None else httpx.Client(follow_redirects=False)
        try:
            try:
                timeout = _remaining_timeout(deadline, self.settings.chat_timeout_seconds, RetrievalError)
                response = client.post(self.endpoint, json=body, headers={"Authorization": f"Bearer {key}"},
                                       timeout=timeout, follow_redirects=False)
                response.raise_for_status()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError):
                raise RetrievalError("RERANK_UNAVAILABLE") from None
            _remaining_timeout(deadline, self.settings.chat_timeout_seconds, RetrievalError)
            try:
                payload = _strict_json(response.text)
                rows = payload["output"]["results"]
                if not isinstance(rows, list) or len(rows) != len(chunks):
                    raise ValueError("missing ranking rows")
                seen, result = set(), []
                for row in rows:
                    index, score = row["index"], row["relevance_score"]
                    if (type(index) is not int or not 0 <= index < len(chunks) or index in seen
                            or type(score) not in (int, float) or not math.isfinite(score)):
                        raise ValueError("invalid ranking row")
                    seen.add(index)
                    result.append({**chunks[index], "rerank_score": score})
                usage = _usage_fields(payload)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError):
                raise RetrievalError("RERANK_INVALID_RESPONSE") from None
            _remaining_timeout(deadline, self.settings.chat_timeout_seconds, RetrievalError)
            self.last_usage = {"model": self.model, "estimated_input_tokens": input_bound,
                               "candidate_count": len(chunks), "latency_seconds": time.monotonic() - started, **usage}
            return result
        finally:
            if owned:
                client.close()
