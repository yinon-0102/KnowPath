"""Embeddings use HTTP doubles; vector filtering uses the real local Qdrant engine."""
import copy
import importlib
import json
import os
from uuid import uuid4
import httpx
import pytest
from qdrant_client import QdrantClient
from my_agent_llms.learning.config import LearningSettings


def api():
    return importlib.import_module("my_agent_llms.learning.vector_retrieval")


def vector(axis=0):
    value = [0.0] * 1024
    value[axis] = 1.0
    return value


def source(identifier, text="Reusable functions", version="version-1", graph=1):
    return {"chunk_id": identifier, "material_id": "material-1", "material_version_id": version,
            "graph_version": graph, "topic_id": "topic-functions", "topic_name": "Functions", "text": text,
            "page": None, "line_start": 1, "line_end": 2}


class Embedder:
    settings = LearningSettings()
    def __init__(self):
        self.calls = []
    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        return [vector(0 if query or "Reusable" in text else 1) for text in texts]


def test_embedding_batches_reorders_indexes_and_uses_fixed_profile(monkeypatch):
    m = api()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    batches = []
    def handle(request):
        body = json.loads(request.content)
        batches.append(body)
        assert body["model"] == "text-embedding-v3"
        assert body["dimensions"] == 1024
        assert body["encoding_format"] == "float"
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vector(i % 2)} for i in reversed(range(len(body["input"])))]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = m.DashScopeEmbedder(client=client).embed(["text " + str(i) for i in range(13)])
    assert [len(b["input"]) for b in batches] == [10, 3]
    assert result[0] == vector(0) and result[1] == vector(1)


@pytest.mark.parametrize("data", [[], [{"index": 0, "embedding": [1.0]}],
    [{"index": 0, "embedding": [0.0] * 1024}],
    [{"index": 0, "embedding": [True] * 1024}],
    [{"index": True, "embedding": vector()}],
    [{"index": -1, "embedding": vector()}],
    [{"index": 0, "embedding": vector()}, {"index": 0, "embedding": vector()}]])
def test_embedding_rejects_malformed_vectors(monkeypatch, data):
    m = api()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": data}))) as client:
        with pytest.raises(m.RetrievalError, match="EMBEDDING_INVALID_RESPONSE"):
            m.DashScopeEmbedder(client=client).embed(["text"])


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_embedding_provider_errors_are_redacted(monkeypatch, status):
    m = api()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="test-secret"))) as client:
        with pytest.raises(m.RetrievalError) as error:
            m.DashScopeEmbedder(client=client).embed(["text"])
    assert str(error.value) == "EMBEDDING_UNAVAILABLE"


def test_embedding_missing_key_does_not_call_provider(monkeypatch):
    m = api()
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(m.RetrievalError, match="EMBEDDING_UNAVAILABLE"):
        m.DashScopeEmbedder().embed(["text"])


@pytest.fixture(params=["local", "server"])
def index(request):
    m = api()
    prefix = "test_learning_vector_" + uuid4().hex
    if request.param == "server":
        url = os.getenv("LEARNING_TEST_QDRANT_URL")
        if not url:
            pytest.skip("LEARNING_TEST_QDRANT_URL is not set")
        client = QdrantClient(url=url, timeout=10, check_compatibility=False)
    else:
        client = QdrantClient(":memory:")
    embedder = Embedder()
    backend = m.QdrantVectorBackend(client, embedder.settings, collection_prefix=prefix)
    try:
        yield m, client, embedder, backend, m.VectorRetriever(embedder, backend)
    finally:
        # Only remove the unique collection owned by this test invocation.
        if client.collection_exists(backend.collection):
            client.delete_collection(backend.collection)
        client.close()


def test_index_is_idempotent_and_retrieval_uses_authoritative_source_text(index):
    m, client, embedder, backend, retriever = index
    rows = [source("a"), source("b", "Unrelated syntax")]
    retriever.index(rows)
    retriever.index(rows)
    assert client.count(backend.collection).count == 2
    points, _ = client.scroll(backend.collection, with_payload=True)
    assert all("text" not in p.payload for p in points)
    assert retriever.select("how to reuse behavior", rows, limit=1) == [rows[0]]
    assert embedder.calls[-1] == (["how to reuse behavior"], True)


def test_retrieval_does_not_cross_versions_graphs_scope_or_content(index):
    m, client, embedder, backend, retriever = index
    allowed = source("a", "Unrelated syntax")
    outsiders = [source("a", version="version-2"), source("a", graph=2), source("b"), source("a", text="Changed content")]
    retriever.index([allowed, *outsiders])
    assert retriever.select("reuse", [allowed]) == [allowed]
    assert client.count(backend.collection).count == 5


def test_partial_index_fails_instead_of_silently_ranking_subset(index):
    m, client, embedder, backend, retriever = index
    rows = [source("a"), source("b")]
    retriever.index(rows[:1])
    with pytest.raises(m.RetrievalError, match="VECTOR_INDEX_NOT_READY"):
        retriever.select("reuse", rows)


def test_missing_index_does_not_create_a_collection_or_call_embedder(index):
    m, client, embedder, backend, retriever = index
    with pytest.raises(m.RetrievalError, match="VECTOR_INDEX_NOT_READY"):
        retriever.select("reuse", [source("a")])
    assert not client.collection_exists(backend.collection)
    assert embedder.calls == []


def test_wrong_existing_collection_shape_is_not_overwritten(index):
    from qdrant_client.models import VectorParams, Distance
    m, client, embedder, backend, retriever = index
    client.create_collection(backend.collection, vectors_config=VectorParams(size=3, distance=Distance.DOT))
    with pytest.raises(m.RetrievalError, match="VECTOR_PROFILE_MISMATCH"):
        retriever.index([source("a")])
    assert client.get_collection(backend.collection).config.params.vectors.size == 3


def test_profile_changes_use_different_collection_names(index):
    from dataclasses import replace
    m, client, embedder, backend, retriever = index
    other = m.QdrantVectorBackend(client, replace(embedder.settings, embedding_model="another-model"))
    assert other.collection != backend.collection


def test_index_failure_is_redacted_and_does_not_report_success(index, monkeypatch):
    m, client, embedder, backend, retriever = index
    def fail(*args, **kwargs):
        raise RuntimeError("test-secret")
    monkeypatch.setattr(client, "upsert", fail)
    with pytest.raises(m.RetrievalError, match="VECTOR_UNAVAILABLE"):
        retriever.index([source("a")])


@pytest.mark.parametrize("magnitude", [1e-100, 1e-30, 1e20, 1e40])
def test_rejects_vectors_whose_float32_norm_would_underflow_or_overflow(index, magnitude):
    m, client, embedder, backend, retriever = index
    invalid = [magnitude] + [0.0] * 1023
    with pytest.raises(m.RetrievalError, match="EMBEDDING_INVALID_RESPONSE"):
        backend.upsert([source("invalid")], [invalid])
    assert not client.collection_exists(backend.collection)
