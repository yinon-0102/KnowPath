"""Provider-neutral model ports and the default compatible DashScope adapter."""
from contextlib import contextmanager
import json
import os
from typing import Protocol, Iterator
import httpx


class ModelError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ChatModel(Protocol):
    def generate(self, messages: list[dict], *, tools=None) -> dict: ...
    def generate_json(self, messages: list[dict]) -> dict: ...
    def stream(self, messages: list[dict]) -> Iterator[str]: ...


class EmbeddingModel(Protocol):
    @property
    def dimension(self) -> int: ...
    @property
    def model_version(self) -> str: ...
    def embed(self, texts: list[str], *, query=False) -> list[list[float]]: ...


class DashScopeChatAdapter:
    capabilities = frozenset({"text", "json", "stream", "tools"})
    models = frozenset({"qwen-plus", "qwen-turbo", "qwen-max"})

    def __init__(self, settings, *, client=None):
        if settings.chat_model not in self.models:
            raise ModelError("UNSUPPORTED_MODEL")
        self.settings, self.client = settings, client

    @contextmanager
    def _client(self):
        if self.client is not None:
            yield self.client
        else:
            with httpx.Client(timeout=self.settings.chat_timeout_seconds, follow_redirects=False) as client:
                yield client

    def _headers(self):
        key = os.getenv(self.settings.chat_api_key_env, "").strip()
        if not key:
            raise ModelError("MODEL_UNAVAILABLE")
        return {"Authorization": "Bearer " + key}

    def _body(self, messages, **options):
        return {"model": self.settings.chat_model, "enable_thinking": False, "messages": messages, **options}

    def _request(self, messages, **options):
        headers = self._headers()
        with self._client() as client:
            for attempt in range(3):
                try:
                    response = client.post(self.settings.chat_base_url.rstrip("/") + "/chat/completions",
                        json=self._body(messages, **options), headers=headers,
                        timeout=self.settings.chat_timeout_seconds, follow_redirects=False)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        continue
                    if response.status_code == 429:
                        raise ModelError("RATE_LIMITED")
                    response.raise_for_status()
                    choice = response.json()["choices"][0]
                    if not isinstance(choice, dict):
                        raise ValueError()
                    return choice
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt < 2:
                        continue
                    raise ModelError("MODEL_UNAVAILABLE") from None
                except (httpx.HTTPError, httpx.InvalidURL):
                    raise ModelError("MODEL_UNAVAILABLE") from None
                except (ValueError, KeyError, IndexError, TypeError):
                    raise ModelError("MODEL_INVALID_RESPONSE") from None

    def generate(self, messages, *, tools=None):
        result = self._request(messages, **({"tools": tools} if tools else {}))
        if result.get("finish_reason") not in {None, "stop", "tool_calls"} or not isinstance(result.get("message"), dict):
            raise ModelError("MODEL_INVALID_RESPONSE")
        return result["message"]

    def generate_json(self, messages):
        result = self._request(messages, response_format={"type": "json_object"})
        try:
            if result.get("finish_reason") not in {None, "stop"}:
                raise ValueError()
            value = json.loads(result["message"]["content"])
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise ModelError("MODEL_INVALID_RESPONSE") from None

    def stream(self, messages):
        headers = self._headers()
        try:
            with self._client() as client:
                with client.stream("POST", self.settings.chat_base_url.rstrip("/") + "/chat/completions",
                                   json=self._body(messages, stream=True), headers=headers,
                                   timeout=self.settings.chat_timeout_seconds, follow_redirects=False) as response:
                    if response.status_code == 429:
                        raise ModelError("RATE_LIMITED")
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        chunk = json.loads(data)
                        for choice in chunk.get("choices", []):
                            if choice.get("finish_reason") not in {None, "stop", "tool_calls"}:
                                raise ModelError("MODEL_INVALID_RESPONSE")
                            content = choice.get("delta", {}).get("content")
                            if content:
                                if not isinstance(content, str):
                                    raise ModelError("MODEL_INVALID_RESPONSE")
                                yield content
                    raise ModelError("MODEL_INVALID_RESPONSE")
        except (httpx.HTTPError, httpx.InvalidURL):
            raise ModelError("MODEL_UNAVAILABLE") from None
        except (ValueError, TypeError, AttributeError):
            raise ModelError("MODEL_INVALID_RESPONSE") from None


_CHAT_ADAPTERS = {"dashscope": DashScopeChatAdapter}
_EMBEDDING_ADAPTERS = {}


def register_chat_adapter(provider, factory):
    _CHAT_ADAPTERS[provider] = factory


def register_embedding_adapter(provider, factory):
    _EMBEDDING_ADAPTERS[provider] = factory


def chat_model(settings, *, client=None):
    factory = _CHAT_ADAPTERS.get(settings.chat_provider)
    if factory is None:
        raise ModelError("UNSUPPORTED_MODEL")
    return factory(settings, client=client)


def embedding_model(settings, *, client=None):
    factory = _EMBEDDING_ADAPTERS.get(settings.embedding_provider)
    if factory is None and settings.embedding_provider == "dashscope":
        from .vector_retrieval import DashScopeEmbedder
        factory = DashScopeEmbedder
    if factory is None:
        raise ModelError("UNSUPPORTED_MODEL")
    try:
        return factory(settings, client=client)
    except ValueError:
        raise ModelError("UNSUPPORTED_MODEL") from None
