"""Configuration defaults for the learning service boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
import secrets
import tempfile
import json


@dataclass(frozen=True)
class LearningSettings:
    chat_provider: str = "dashscope"
    chat_model: str = "qwen-plus"
    embedding_provider: str = "dashscope"
    embedding_model: str = "text-embedding-v3"
    embedding_dimension: int = 1024
    chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_api_key_env: str = "DASHSCOPE_API_KEY"
    chat_timeout_seconds: float = 60.0
    context_budget_tokens: int = 16000
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key_env: str = "DASHSCOPE_API_KEY"
    local_token: str | None = None
    allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000")

    @classmethod
    def from_env(cls) -> "LearningSettings":
        configuration = {}
        if os.getenv("LEARNING_CONFIG_FILE"):
            configuration = json.loads(Path(os.environ["LEARNING_CONFIG_FILE"]).read_text(encoding="utf-8"))
            if not isinstance(configuration, dict):
                raise ValueError("Learning configuration must be an object")
        chat, embedding = configuration.get("chat", {}), configuration.get("embedding", {})
        if not isinstance(chat, dict) or not isinstance(embedding, dict):
            raise ValueError("Chat and embedding configuration must be objects")
        def setting(env, values, field, default):
            return os.getenv(env, values.get(field, default))
        dimension = int(setting("LEARNING_EMBEDDING_DIMENSION", embedding, "dimension", 1024))
        timeout = float(setting("LEARNING_CHAT_TIMEOUT_SECONDS", chat, "timeout_seconds", 60))
        context_budget = int(setting("LEARNING_CONTEXT_BUDGET_TOKENS", chat, "context_budget_tokens", 16000))
        if not 1024 <= context_budget <= 65536:
            raise ValueError("Context budget must be between 1024 and 65536 tokens")
        if dimension <= 0 or not 0 < timeout <= 300:
            raise ValueError("Invalid model dimension or timeout")
        return cls(
            chat_provider=setting("LEARNING_CHAT_PROVIDER", chat, "provider", "dashscope"),
            chat_model=setting("LEARNING_CHAT_MODEL", chat, "model", "qwen-plus"),
            chat_base_url=setting("LEARNING_CHAT_BASE_URL", chat, "base_url", cls.chat_base_url),
            chat_api_key_env=setting("LEARNING_CHAT_API_KEY_ENV", chat, "api_key_env", "DASHSCOPE_API_KEY"),
            chat_timeout_seconds=timeout,
            context_budget_tokens=context_budget,
            embedding_provider=setting("LEARNING_EMBEDDING_PROVIDER", embedding, "provider", "dashscope"),
            embedding_model=setting("LEARNING_EMBEDDING_MODEL", embedding, "model", "text-embedding-v3"),
            embedding_dimension=dimension,
            embedding_base_url=setting("LEARNING_EMBEDDING_BASE_URL", embedding, "base_url", cls.embedding_base_url),
            embedding_api_key_env=setting("LEARNING_EMBEDDING_API_KEY_ENV", embedding, "api_key_env", "DASHSCOPE_API_KEY"),
            local_token=os.getenv("LEARNING_LOCAL_TOKEN") or None,
            allowed_origins=tuple(origin.strip() for origin in os.getenv("LEARNING_ALLOWED_ORIGINS", ",".join(cls.allowed_origins)).split(",") if origin.strip()),
        )



def server_settings() -> LearningSettings:
    """The deployed entrypoint always authenticates; factories allow injected test settings."""
    settings = LearningSettings.from_env()
    if settings.local_token:
        return settings
    path = Path(os.getenv("LEARNING_LOCAL_TOKEN_FILE") or Path(__file__).resolve().parents[2] / ".learning-token.local")
    # Publish a fully written file atomically. A concurrent process either wins
    # creation or reads the winner's complete token; it cannot see an empty file.
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".learning-token-", suffix=".local", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError("Local session token file is invalid")
    return replace(settings, local_token=token)
