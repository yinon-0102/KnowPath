"""Configuration defaults for the learning service boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LearningSettings:
    chat_provider: str = "dashscope"
    chat_model: str = "qwen-plus"
    embedding_provider: str = "dashscope"
    embedding_model: str = "text-embedding-v3"
    embedding_dimension: int = 1024
    local_token: str | None = None

    @classmethod
    def from_env(cls) -> "LearningSettings":
        raw_dimension = os.getenv("LEARNING_EMBEDDING_DIMENSION", "1024")
        try:
            dimension = int(raw_dimension)
        except ValueError as exc:
            raise ValueError("LEARNING_EMBEDDING_DIMENSION must be an integer") from exc
        if dimension <= 0:
            raise ValueError("LEARNING_EMBEDDING_DIMENSION must be positive")
        return cls(
            chat_provider=os.getenv("LEARNING_CHAT_PROVIDER", "dashscope"),
            chat_model=os.getenv("LEARNING_CHAT_MODEL", "qwen-plus"),
            embedding_provider=os.getenv("LEARNING_EMBEDDING_PROVIDER", "dashscope"),
            embedding_model=os.getenv("LEARNING_EMBEDDING_MODEL", "text-embedding-v3"),
            embedding_dimension=dimension,
            local_token=os.getenv("LEARNING_LOCAL_TOKEN") or None,
        )
