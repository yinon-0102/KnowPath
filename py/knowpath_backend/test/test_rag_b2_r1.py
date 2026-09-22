"""Tests for the evaluation-only B2-R1 continuation closure plugin."""
import importlib
import time

import pytest

from knowpath_backend.learning.rag.contracts import RetrievalBudget, RetrievalRequest


def row(identifier, text="alpha", *, unit=None, version="m1", retrieval="r1", parent="p1",
        ordinal=0, token_count=1, tree_version="tree-1"):
    return {
        "chunk_id": identifier,
        "material_version_id": version,
        "retrieval_version_id": retrieval,
        "source_text": text,
        "retrieval_text": text,
        "parent_id": parent,
        "ordinal": ordinal,
        "continuation_of": unit,
        "tree_version_id": tree_version,
        "quality": {"structure": "known", "token_count": token_count},
        "source_spans": [{"material_version_id": version, "artifact_hash": "a" * 64,
                          "start": 0, "end": len(text)}],
    }


def request(**budget):
    return RetrievalRequest(query="alpha", original_query="alpha", scope_snapshot_id="scope",
                            manifest_ids=("manifest",), budget=RetrievalBudget(**budget),
                            deadline=time.monotonic() + 30)


class Embedder:
    model_version = "test-model"
    dimension = 3

    def __init__(self):
        self.calls = []
        self.last_usage = None

    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        return [[1.0, 0.0, 0.0]]


class Dense:
    collection = "test-collection"

    def search(self, vector, chunks, limit):
        return [(row["chunk_id"], 1.0) for row in chunks[:limit]]

    def verify(self, rows):
        return {"verified": True}


def plugin(monkeypatch, *, fused, embedder=None):
    module = importlib.import_module("knowpath_backend.learning.rag.continuation")
    instance = module.ContinuationClosurePlugin(embedder or Embedder(), Dense())

    def fixed_rankings(request, rows, *, query_vector=None, total_limit=None):
        allowed = {row["chunk_id"] for row in rows}
        return {"keyword": [], "dense": [],
                "fused": [(identifier, score) for identifier, score in fused if identifier in allowed],
                "query_vector": [1.0, 0.0, 0.0], "embedding_calls": 1,
                "bm25_cache_hit": False}

    monkeypatch.setattr(instance, "_hybrid_rankings", fixed_rankings)
    return instance


def ids(result):
    return [candidate["chunk_id"] for candidate in result["candidates"]]


def test_continuation_closure_emits_complete_unit_in_ordinal_order(monkeypatch):
    rows = [row("u1", "alpha first", ordinal=0),
            row("u2", "alpha second", unit="u1", ordinal=1),
            row("other", "alpha unrelated", ordinal=2)]
    embedder = Embedder()
    result = plugin(monkeypatch, fused=[("u1", 1.0), ("other", .5)], embedder=embedder).retrieve(
        request(rerank_candidates=3, context_tokens=20), rows)

    assert ids(result) == ["u1", "u2", "other"]
    assert [entry["chunk_id"] for entry in result["trace"]["closures"]] == ["u2"]
    assert all(entry["edge_type"] == "continuation" for entry in result["trace"]["closures"])
    assert result["trace"]["structural_additions"] == 1
    assert result["trace"]["a_retention_at_40"] == 1.0


def test_continuation_closure_keeps_seed_when_complete_unit_does_not_fit(monkeypatch):
    rows = [row("u1", "alpha first", ordinal=0),
            row("u2", "alpha second", unit="u1", ordinal=1),
            row("other", "alpha unrelated", ordinal=2)]
    result = plugin(monkeypatch, fused=[("u1", 1.0), ("other", .5)]).retrieve(
        request(rerank_candidates=1, context_tokens=20), rows)

    assert ids(result) == ["u1"]
    assert result["trace"]["closures"] == []
    assert result["trace"]["skipped_closures"] == [{"unit_id": "u1", "reason": "candidate_budget"}]


def test_continuation_closure_deduplicates_repeated_seed_units(monkeypatch):
    rows = [row("u1", "alpha first", ordinal=0),
            row("u2", "alpha second", unit="u1", ordinal=1),
            row("other", "alpha unrelated", ordinal=2)]
    result = plugin(monkeypatch, fused=[("u1", 1.0), ("u2", .9), ("other", .5)]).retrieve(
        request(rerank_candidates=4, context_tokens=20), rows)

    assert ids(result) == ["u1", "u2", "other"]
    assert len(ids(result)) == len(set(ids(result)))


def test_continuation_closure_rejects_cross_version_unit(monkeypatch):
    rows = [row("u1", "alpha first", ordinal=0),
            row("u2", "alpha second", unit="u1", ordinal=1, version="m2")]
    result = plugin(monkeypatch, fused=[("u1", 1.0)]).retrieve(
        request(rerank_candidates=4, context_tokens=20), rows)

    assert ids(result) == ["u1"]
    assert result["trace"]["structure_unavailable"] == "STRUCTURE_UNAVAILABLE"


def test_continuation_closure_skips_unit_that_exceeds_context_budget(monkeypatch):
    rows = [row("u1", "alpha first", ordinal=0, token_count=8),
            row("u2", "alpha second", unit="u1", ordinal=1, token_count=8)]
    result = plugin(monkeypatch, fused=[("u1", 1.0)]).retrieve(
        request(rerank_candidates=4, context_tokens=10), rows)

    assert ids(result) == ["u1"]
    assert result["trace"]["skipped_closures"] == [{"unit_id": "u1", "reason": "context_budget"}]
