"""Connection settings for the graph and vector stores.

Clients are created by infrastructure wiring, so domain code never branches on
Neo4j or Qdrant SDK details.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GraphSettings:
    uri: str = "bolt://127.0.0.1:7687"
    username: str = "neo4j"
    password: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "GraphSettings":
        password = os.getenv("NEO4J_PASSWORD", "")
        if not password.strip():
            raise ValueError("NEO4J_PASSWORD is required")
        return cls(
            uri=os.getenv("NEO4J_URI", cls.uri),
            username=os.getenv("NEO4J_USERNAME", cls.username),
            password=password,
        )


@dataclass(frozen=True)
class VectorSettings:
    url: str = "http://127.0.0.1:6333"
    collection: str = "keel_material_chunks"
    distance: str = "Cosine"
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "VectorSettings":
        return cls(
            url=os.getenv("QDRANT_URL", cls.url),
            collection=os.getenv("QDRANT_COLLECTION", cls.collection),
            distance=os.getenv("QDRANT_DISTANCE", cls.distance),
            api_key=os.getenv("QDRANT_API_KEY") or None,
        )
