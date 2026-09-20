"""Hybrid plugin boundary, model calls, cache, and deadline behavior."""
import importlib
import time

import pytest

from knowpath_backend.learning.rag.contracts import RetrievalBudget, RetrievalRequest


def api():
    return importlib.import_module("knowpath_backend.learning.rag.plugins")


def chunk(identifier, text="alpha", *, version="m1", parent="p1", ordinal=0, **extra):
    return {"chunk_id": identifier, "material_version_id": version, "retrieval_version_id": "r1",
            "source_text": text, "retrieval_text": text, "parent_id": parent, "ordinal": ordinal,
            "source_spans": [{"material_version_id": version, "artifact_hash": "a" * 64,
                              "start": 0, "end": len(text)}], **extra}


def request(query="alpha", **budget):
    return RetrievalRequest(query=query, original_query=query, scope_snapshot_id="scope",
                            manifest_ids=("manifest",), budget=RetrievalBudget(**budget), deadline=time.monotonic() + 30)


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query=False):
        self.calls.append((texts, query))
        return [[1., 0., 0.]]


class Dense:
    def __init__(self, ranked=None):
        self.ranked = ranked
        self.calls = []

    def search(self, vector, chunks, limit):
        self.calls.append((vector, list(chunks), limit))
        return [(identifier, 1.) for identifier in (self.ranked or [c["chunk_id"] for c in chunks])[:limit]]


def test_ordinary_sends_only_query_to_embedder_and_only_scope_to_search():
    embedder, dense = Embedder(), Dense()
    plugin = api().OrdinaryPlugin(embedder, dense)
    rows = [chunk("allowed", "source secret alpha")]
    result = plugin.retrieve(request(), rows)
    assert embedder.calls == [(["alpha"], True)]
    assert [c["chunk_id"] for c in dense.calls[0][1]] == ["allowed"]
    assert result["candidates"][0]["chunk_id"] == "allowed"
    assert result["candidates"][0]["channel"] == "hybrid"
    assert not {"source_text", "retrieval_text", "source_spans"} & result["candidates"][0].keys()


def test_empty_scope_does_not_embed():
    embedder, dense = Embedder(), Dense()
    assert api().OrdinaryPlugin(embedder, dense).retrieve(request(), [])["candidates"] == []
    assert embedder.calls == dense.calls == []


@pytest.mark.parametrize("rows", [[chunk("a"), chunk("a")], [chunk("a", source_spans=[])],
    [chunk("a", source_spans=[{"material_version_id": "other", "artifact_hash": "a" * 64, "start": 0, "end": 1}])],
    [chunk("a", retrieval_version_id="")]])
def test_invalid_scope_rejected_before_model_calls(rows):
    embedder = Embedder()
    with pytest.raises(Exception, match="RETRIEVAL_SCOPE_INVALID"):
        api().OrdinaryPlugin(embedder, Dense()).retrieve(request(), rows)
    assert embedder.calls == []


def test_channel_cannot_return_excluded_identifier():
    with pytest.raises(Exception, match="RETRIEVAL_INVALID_RESPONSE"):
        api().OrdinaryPlugin(Embedder(), Dense(["excluded"])).retrieve(request(), [chunk("allowed")])


def test_bm25_cache_reuses_scope_across_questions_and_evicts_lru(monkeypatch):
    m = api()
    real = m.BM25Index
    built = []
    def build(rows, profile=None):
        built.append([r["chunk_id"] for r in rows])
        return real(rows, profile)
    monkeypatch.setattr(m, "BM25Index", build)
    plugin = m.OrdinaryPlugin(Embedder(), Dense(), cache_size=2)
    rows = [chunk("a")]
    first = plugin.retrieve(request("alpha"), rows)
    second = plugin.retrieve(request("beta"), list(reversed(rows)))
    assert len(built) == 1
    assert not first["trace"]["bm25_cache_hit"] and second["trace"]["bm25_cache_hit"]
    plugin.retrieve(request(), [chunk("b")])
    plugin.retrieve(request(), [chunk("c")])
    plugin.retrieve(request(), rows)
    assert len(built) == 4
    plugin.retrieve(request(), [chunk("a", "changed title alpha")])
    assert len(built) == 5


def test_budget_channels_are_separate_and_final_rerank_cap_is_forty():
    dense = Dense()
    rows = [chunk(f"a{i:02}") for i in range(70)]
    result = api().OrdinaryPlugin(Embedder(), dense).retrieve(request(keyword_candidates=60, vector_candidates=50, rerank_candidates=100), rows)
    assert dense.calls[0][2] == 50
    assert len(result["candidates"]) == 40
    assert result["trace"]["keyword_count"] == 60


def test_expired_deadline_stops_before_embedding():
    embedder = Embedder()
    expired = request().model_copy(update={"deadline": time.monotonic() - 1})
    with pytest.raises(Exception, match="RETRIEVAL_DEADLINE_EXCEEDED"):
        api().OrdinaryPlugin(embedder, Dense()).retrieve(expired, [chunk("a")])
    assert embedder.calls == []


@pytest.mark.parametrize("stage", ["embed", "search"])
def test_deadline_checked_after_external_call(monkeypatch, stage):
    m = api()
    now = [10.]
    monkeypatch.setattr(m.time, "monotonic", lambda: now[0])
    embedder, dense = Embedder(), Dense()
    target = embedder if stage == "embed" else dense
    original = getattr(target, stage)
    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 50.
        return result
    monkeypatch.setattr(target, stage, late)
    with pytest.raises(Exception, match="RETRIEVAL_DEADLINE_EXCEEDED"):
        m.OrdinaryPlugin(embedder, dense).retrieve(request().model_copy(update={"deadline": 20.}), [chunk("a")])
    assert len(dense.calls) == (0 if stage == "embed" else 1)


@pytest.mark.parametrize("ranking", [[("a", float("nan"))], [("a", 1.), ("a", 2.)], [("a", True)], [None], [("a",)]])
def test_malformed_channel_response_is_rejected(monkeypatch, ranking):
    dense = Dense()
    monkeypatch.setattr(dense, "search", lambda *a, **k: ranking)
    with pytest.raises(Exception, match="RETRIEVAL_INVALID_RESPONSE"):
        api().OrdinaryPlugin(Embedder(), dense).retrieve(request(), [chunk("a")])


def test_model_failure_propagates_without_fallback_or_generation(monkeypatch):
    m = api()
    embedder, dense = Embedder(), Dense()
    def fail(*a, **k):
        raise m.RetrievalError("EMBEDDING_UNAVAILABLE")
    monkeypatch.setattr(embedder, "embed", fail)
    with pytest.raises(m.RetrievalError, match="EMBEDDING_UNAVAILABLE"):
        m.OrdinaryPlugin(embedder, dense).retrieve(request(), [chunk("a")])
    assert dense.calls == []


def test_real_qdrant_hybrid_only_returns_authorized_scope():
    from qdrant_client import QdrantClient
    from knowpath_backend.learning.rag.vector import QdrantContentIndex
    client = QdrantClient(":memory:")
    try:
        dense = QdrantContentIndex(client, "plugin_test", 3, "fake-v1")
        allowed, excluded = chunk("allowed", "alpha"), chunk("excluded", "alpha alpha")
        dense.upsert([allowed, excluded], [[0., 1., 0.], [1., 0., 0.]])
        result = api().OrdinaryPlugin(Embedder(), dense).retrieve(request(), [allowed])
        assert [c["chunk_id"] for c in result["candidates"]] == ["allowed"]
    finally:
        client.close()


@pytest.mark.parametrize("profile", [[], "wrong", {"tokenizer": "wrong"}, {"k1": float("nan")}])
def test_invalid_bm25_profile_is_rejected_before_query_embedding(profile):
    embedder = Embedder()
    with pytest.raises(ValueError):
        api().OrdinaryPlugin(embedder, Dense(), bm25_profile=profile).retrieve(request(), [chunk("a")])
    assert embedder.calls == []
