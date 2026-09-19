"""Rebuildable vectors; source text and authorization always come from SQL snapshots."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from uuid import UUID

import httpx
from qdrant_client import QdrantClient, models

from .config import LearningSettings


class RetrievalError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(source):
    identity = [source["material_id"], source["material_version_id"], source["graph_version"],
                source["chunk_id"], content_hash(source["text"])]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).digest()
    return str(UUID(bytes=digest[:16]))


def valid_vector(value, dimension):
    if not isinstance(value, list) or len(value) != dimension:
        return False
    try:
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
            return False
        squared_norm = sum(n * n for n in value)
        # Qdrant computes Cosine using float32. A finite double-precision
        # vector can otherwise become zero/NaN during conversion or norm.
        return 1.1754943508222875e-38 <= squared_norm <= 3.4028234663852886e38
    except (OverflowError, TypeError, ValueError):
        return False


def normalized(vector):
    norm = math.hypot(*vector)
    return [value / norm for value in vector]


class DashScopeEmbedder:
    def __init__(self, settings=None, *, client=None):
        self.settings = settings or LearningSettings.from_env()
        self.client = client
        if (self.settings.embedding_provider, self.settings.embedding_model, self.settings.embedding_dimension) != (
                "dashscope", "text-embedding-v3", 1024):
            raise ValueError("vector retrieval currently requires DashScope text-embedding-v3 / 1024")

    @property
    def dimension(self):
        return self.settings.embedding_dimension

    @property
    def model_version(self):
        return self.settings.embedding_model

    def embed(self, texts, *, query=False):
        if not texts:
            return []
        if any(not isinstance(t, str) or not t.strip() or len(t) > 8000 for t in texts):
            raise RetrievalError("EMBEDDING_INPUT_INVALID")
        key = os.getenv(self.settings.embedding_api_key_env, "").strip()
        if not key:
            raise RetrievalError("EMBEDDING_UNAVAILABLE")
        base = self.settings.embedding_base_url.rstrip("/")
        owned = self.client is None
        client = self.client if self.client is not None else httpx.Client(timeout=60.0, follow_redirects=False)
        vectors = []
        try:
            for start in range(0, len(texts), 10):
                batch = texts[start:start + 10]
                body = {"model": self.settings.embedding_model, "input": batch,
                        "dimensions": self.settings.embedding_dimension, "encoding_format": "float"}
                try:
                    response = client.post(base + "/embeddings", json=body,
                        headers={"Authorization": f"Bearer {key}"}, timeout=60.0, follow_redirects=False)
                    if response.status_code == 429:
                        raise RetrievalError("RATE_LIMITED")
                    response.raise_for_status()
                except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                    raise RetrievalError("EMBEDDING_UNAVAILABLE") from None
                try:
                    rows = response.json()["data"]
                    if not isinstance(rows, list) or len(rows) != len(batch):
                        raise ValueError()
                    ordered = {}
                    for row in rows:
                        index, embedding = row["index"], row["embedding"]
                        if (type(index) is not int or not 0 <= index < len(batch) or index in ordered
                                or not valid_vector(embedding, self.settings.embedding_dimension)):
                            raise ValueError()
                        ordered[index] = embedding
                    vectors.extend(ordered[i] for i in range(len(batch)))
                except (ValueError, KeyError, IndexError, TypeError, OverflowError):
                    raise RetrievalError("EMBEDDING_INVALID_RESPONSE") from None
        finally:
            if owned:
                client.close()
        return vectors


class KeywordRetriever:
    def select(self, message, sources, *, limit=8):
        tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", message.casefold()))
        def rank(row):
            searchable = (row["topic_name"] + " " + row["text"]).casefold()
            return (-sum(token in searchable for token in tokens), row["chunk_id"])
        return sorted(sources, key=rank)[:limit]


class QdrantVectorBackend:
    def __init__(self, client, settings=None, *, collection_prefix=None):
        self.client = client
        self.settings = settings or LearningSettings.from_env()
        prefix = collection_prefix or os.getenv("QDRANT_COLLECTION", "keel_material_chunks")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", prefix):
            raise ValueError("QDRANT_COLLECTION must be a short alphanumeric collection prefix")
        profile = ["source-v1", self.settings.embedding_provider, self.settings.embedding_model,
                   self.settings.embedding_dimension, "Cosine"]
        self.profile = hashlib.sha256(json.dumps(profile).encode()).hexdigest()[:16]
        self.collection = prefix + "_" + self.profile

    def _check_collection(self, *, create=False):
        exists = self.client.collection_exists(self.collection)
        if not exists and create:
            try:
                self.client.create_collection(self.collection,
                    vectors_config=models.VectorParams(size=self.settings.embedding_dimension, distance=models.Distance.COSINE))
            except Exception:
                # Concurrent builders may have created the same profile collection.
                if not self.client.collection_exists(self.collection):
                    raise
            exists = True
        if not exists:
            raise RetrievalError("VECTOR_INDEX_NOT_READY")
        config = self.client.get_collection(self.collection).config.params.vectors
        if (not isinstance(config, models.VectorParams) or config.size != self.settings.embedding_dimension
                or config.distance != models.Distance.COSINE):
            raise RetrievalError("VECTOR_PROFILE_MISMATCH")

    def upsert(self, sources, vectors):
        if len(sources) != len(vectors) or any(not valid_vector(v, self.settings.embedding_dimension) for v in vectors):
            raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
        try:
            self._check_collection(create=True)
            points = []
            for row, vector in zip(sources, vectors):
                payload = {key: row[key] for key in ("material_id", "material_version_id", "graph_version", "chunk_id", "topic_id")}
                payload.update(content_hash=content_hash(row["text"]), embedding_model=self.settings.embedding_model,
                               embedding_profile=self.profile)
                points.append(models.PointStruct(id=point_id(row), vector=normalized(vector), payload=payload))
            if points:
                self.client.upsert(self.collection, points=points, wait=True)
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None

    def require_index(self, sources):
        try:
            self._check_collection()
            ids = list(dict.fromkeys(point_id(s) for s in sources))
            for start in range(0, len(ids), 256):
                batch = ids[start:start + 256]
                found = self.client.retrieve(self.collection, ids=batch, with_payload=False, with_vectors=False)
                if {str(p.id) for p in found} != set(batch):
                    raise RetrievalError("VECTOR_INDEX_NOT_READY")
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None

    def search(self, vector, sources, *, limit=8):
        if not valid_vector(vector, self.settings.embedding_dimension):
            raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
        allowed = {point_id(s): s for s in sources}
        try:
            result = self.client.query_points(self.collection, query=normalized(vector), limit=limit,
                query_filter=models.Filter(must=[models.HasIdCondition(has_id=list(allowed)),
                    models.FieldCondition(key="embedding_profile", match=models.MatchValue(value=self.profile))]),
                with_payload=False, with_vectors=False)
            selected, seen = [], set()
            for point in result.points:
                identifier = str(point.id)
                if identifier in allowed and identifier not in seen and math.isfinite(point.score):
                    selected.append(allowed[identifier])
                    seen.add(identifier)
            if len(selected) != min(limit, len(allowed)):
                raise RetrievalError("VECTOR_INDEX_NOT_READY")
            return selected
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None


class VectorRetriever:
    def __init__(self, embedder, backend):
        self.embedder, self.backend = embedder, backend

    def close(self):
        self.backend.client.close()

    def index(self, sources):
        # Bounded writes; failed later batches leave an incomplete, rebuildable index.
        for start in range(0, len(sources), 10):
            batch = sources[start:start + 10]
            self.backend.upsert(batch, self.embedder.embed([s["text"] for s in batch]))
        return {"collection": self.backend.collection, "indexed_chunks": len(sources)}

    def select(self, message, sources, *, limit=8):
        if not sources:
            return []
        if not 1 <= limit <= 100:
            raise ValueError("retrieval limit must be between 1 and 100")
        self.backend.require_index(sources)
        vector = self.embedder.embed([message], query=True)[0]
        return self.backend.search(vector, sources, limit=limit)


class UnsupportedRetriever:
    configuration_error = "UNSUPPORTED_MODEL"

    def select(self, message, sources, *, limit=8):
        raise RetrievalError(self.configuration_error)


def configured_retriever(settings=None):
    mode = os.getenv("LEARNING_RETRIEVAL_BACKEND", "keyword")
    if mode == "keyword":
        return KeywordRetriever()
    if mode != "qdrant":
        raise ValueError("LEARNING_RETRIEVAL_BACKEND must be keyword or qdrant")
    from .model_adapters import ModelError
    try:
        return configured_vector_retriever(settings)
    except ModelError:
        # The HTTP service remains available to explain a configuration error.
        # Background indexing still rejects this configuration at worker startup.
        return UnsupportedRetriever()


def configured_vector_retriever(settings=None):
    settings = settings or LearningSettings.from_env()
    from .model_adapters import embedding_model
    embedder = embedding_model(settings)
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
                          api_key=os.getenv("QDRANT_API_KEY") or None, timeout=30, check_compatibility=False)
    return VectorRetriever(embedder, QdrantVectorBackend(client, settings))
