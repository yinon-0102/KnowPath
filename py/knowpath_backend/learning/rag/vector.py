"""Rebuildable content-v2 vectors independent of legacy graph-version IDs."""
from __future__ import annotations

import hashlib
import json
import math
import re
from uuid import UUID

from qdrant_client import models

from .bm25 import validate_limit
from .retrieval import RetrievalError, content_hash, normalized, valid_vector


def point_id(chunk):
    identity = ["content-v2", chunk["retrieval_version_id"], chunk["chunk_id"],
                chunk["material_version_id"], content_hash(chunk["retrieval_text"])]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).digest()
    return str(UUID(bytes=digest[:16]))


class QdrantContentIndex:
    def __init__(self, client, collection_name, dimension, embedding_profile):
        if not isinstance(collection_name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", collection_name):
            raise ValueError("invalid Qdrant collection prefix")
        if type(dimension) is not int or not 1 <= dimension <= 65536:
            raise ValueError("invalid embedding dimension")
        if not isinstance(embedding_profile, (str, dict)) or not embedding_profile:
            raise ValueError("embedding profile must identify the model and provider")
        if isinstance(embedding_profile, str) and not embedding_profile.strip():
            raise ValueError("embedding profile must not be blank")
        if isinstance(embedding_profile, dict) and "dimension" in embedding_profile:
            if type(embedding_profile["dimension"]) is not int or embedding_profile["dimension"] != dimension:
                raise ValueError("embedding profile dimension mismatch")
        try:
            provenance = json.dumps(embedding_profile, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("invalid embedding profile") from None
        self.client, self.dimension = client, dimension
        self.embedding_profile = json.loads(provenance)
        identity = json.dumps(["content-v2", dimension, "Cosine", provenance], separators=(",", ":"))
        self.profile = hashlib.sha256(identity.encode()).hexdigest()
        self.collection_prefix = collection_name
        self.collection = collection_name + "_v2_" + self.profile[:24]

    def _payload(self, chunk):
        payload = {key: chunk[key] for key in ("retrieval_version_id", "chunk_id", "material_version_id")}
        if any(not isinstance(value, str) or not value for value in payload.values()):
            raise ValueError("content identities must be nonempty strings")
        payload.update(content_hash=content_hash(chunk["retrieval_text"]), embedding_profile=self.profile,
                       embedding_provenance=self.embedding_profile, embedding_dimension=self.dimension)
        return payload

    def _expected(self, chunks):
        expected, chunk_ids = {}, set()
        for row in chunks:
            identifier = point_id(row)
            payload = self._payload(row)
            key = (payload["retrieval_version_id"], payload["chunk_id"])
            if key in chunk_ids:
                raise ValueError("duplicate versioned chunk ID")
            chunk_ids.add(key)
            expected[identifier] = payload
        return expected

    def _check_collection(self, create=False):
        exists = self.client.collection_exists(self.collection)
        if not exists and create:
            try:
                self.client.create_collection(self.collection, vectors_config=models.VectorParams(size=self.dimension, distance=models.Distance.COSINE))
            except Exception:
                if not self.client.collection_exists(self.collection):
                    raise
            exists = True
        if not exists:
            raise RetrievalError("VECTOR_INDEX_NOT_READY")
        config = self.client.get_collection(self.collection).config.params.vectors
        if not isinstance(config, models.VectorParams) or config.size != self.dimension or config.distance != models.Distance.COSINE:
            raise RetrievalError("VECTOR_PROFILE_MISMATCH")

    def upsert(self, chunks, vectors):
        chunks, vectors = list(chunks), list(vectors)
        if len(chunks) != len(vectors) or any(not valid_vector(v, self.dimension) for v in vectors):
            raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
        expected = self._expected(chunks)
        try:
            self._check_collection(create=True)
            points = [models.PointStruct(id=identifier, vector=normalized(vector), payload=payload)
                      for (identifier, payload), vector in zip(expected.items(), vectors)]
            for start in range(0, len(points), 256):
                result = self.client.upsert(self.collection, points=points[start:start + 256], wait=True)
                if result.status != models.UpdateStatus.COMPLETED:
                    raise RetrievalError('VECTOR_WRITE_UNCONFIRMED')
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None

    def verify(self, chunks):
        expected = self._expected(chunks)
        try:
            self._check_collection()
            ids = list(expected)
            for start in range(0, len(ids), 256):
                batch = ids[start:start + 256]
                records = self.client.retrieve(self.collection, ids=batch, with_payload=True, with_vectors=True)
                if len(records) != len(batch) or {str(p.id) for p in records} != set(batch):
                    raise RetrievalError("VECTOR_INDEX_NOT_READY")
                for record in records:
                    if record.payload != expected[str(record.id)] or not valid_vector(record.vector, self.dimension):
                        raise RetrievalError("VECTOR_INDEX_NOT_READY")
            return {"verified": True, "collection": self.collection, "embedding_profile": self.profile,
                    "chunks": [{"chunk_id": p["chunk_id"], "content_hash": p["content_hash"]} for p in expected.values()]}
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None

    def search(self, vector, chunks, limit=30):
        validate_limit(limit)
        if not valid_vector(vector, self.dimension):
            raise RetrievalError("EMBEDDING_INVALID_RESPONSE")
        expected = self._expected(chunks)
        if not expected:
            return []
        try:
            self._check_collection()
            versions = sorted({p["retrieval_version_id"] for p in expected.values()})
            result = self.client.query_points(self.collection, query=normalized(vector), limit=limit,
                query_filter=models.Filter(must=[models.HasIdCondition(has_id=list(expected)),
                    models.FieldCondition(key="embedding_profile", match=models.MatchValue(value=self.profile)),
                    models.FieldCondition(key="retrieval_version_id", match=models.MatchAny(any=versions))]),
                with_payload=True, with_vectors=False)
            selected, seen = [], set()
            for record in result.points:
                identifier = str(record.id)
                if (identifier not in expected or identifier in seen or type(record.score) not in (int, float)
                        or not math.isfinite(record.score) or record.payload != expected[identifier]):
                    raise RetrievalError("VECTOR_INVALID_RESPONSE")
                seen.add(identifier)
                selected.append((expected[identifier]["chunk_id"], record.score))
            if len(selected) > limit:
                raise RetrievalError("VECTOR_INVALID_RESPONSE")
            return selected
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None

    def delete_retrieval_version(self, retrieval_version_id):
        if not isinstance(retrieval_version_id, str) or not retrieval_version_id:
            raise ValueError("retrieval version must be a nonempty string")
        try:
            if not self.client.collection_exists(self.collection):
                return
            self._check_collection()
            self.client.delete(self.collection, points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="retrieval_version_id", match=models.MatchValue(value=retrieval_version_id)),
                models.FieldCondition(key="embedding_profile", match=models.MatchValue(value=self.profile))])), wait=True)
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError("VECTOR_UNAVAILABLE") from None
