"""Connection settings for the graph and vector stores.

Clients are created by infrastructure wiring, so domain code never branches on
Neo4j or Qdrant SDK details.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphSettings:
    uri: str = "bolt://127.0.0.1:7687"
    username: str = "neo4j"
    password: str = "keel_learning"

    @classmethod
    def from_env(cls) -> "GraphSettings":
        return cls(
            uri=os.getenv("NEO4J_URI", cls.uri),
            username=os.getenv("NEO4J_USERNAME", cls.username),
            password=os.getenv("NEO4J_PASSWORD", cls.password),
        )


@dataclass(frozen=True)
class VectorSettings:
    url: str = "http://127.0.0.1:6333"
    collection: str = "keel_material_chunks"
    distance: str = "Cosine"

    @classmethod
    def from_env(cls) -> "VectorSettings":
        return cls(
            url=os.getenv("QDRANT_URL", cls.url),
            collection=os.getenv("QDRANT_COLLECTION", cls.collection),
            distance=os.getenv("QDRANT_DISTANCE", cls.distance),
        )
