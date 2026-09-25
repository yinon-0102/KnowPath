"""Tests for B3 unit-first retrieval contracts and admission rules."""
import time

import pytest

from knowpath_backend.learning.rag.contracts import RetrievalBudget, RetrievalRequest
from knowpath_backend.test.test_rag_building import build_workspace


def leaf(identifier, text="alpha", *, unit=None, version="m1", retrieval="r1", parent="p1",
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
                          "start": ordinal * 10, "end": ordinal * 10 + len(text)}],
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

    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        return [[1.0, 0.0, 0.0]]


class Dense:
    collection = "test-collection"

    def search(self, vector, chunks, limit=30):
        return [(row["chunk_id"], 1.0) for row in chunks[:limit]]

    def verify(self, rows):
        return {"verified": True}


def test_build_unit_is_order_invariant_and_preserves_leaf_source_map():
    from knowpath_backend.learning.rag.units import build_unit

    root = leaf("u1", "alpha first", ordinal=0)
    continuation = leaf("u2", "alpha second", unit="u1", ordinal=1)
    forward = build_unit([root, continuation], tree_version_id="tree-1")
    reverse = build_unit([continuation, root], tree_version_id="tree-1")

    assert forward.unit_id == reverse.unit_id
    assert forward.leaf_ids == ("u1", "u2")
    assert forward.source_text == "alpha first\nalpha second"
    assert len(forward.source_spans) == 2
    assert forward.source_map_hash


@pytest.mark.parametrize("field", ["material_version_id", "retrieval_version_id", "parent_id", "tree_version_id"])
def test_build_unit_rejects_mixed_identity(field):
    from knowpath_backend.learning.rag.units import build_unit

    root = leaf("u1", ordinal=0)
    continuation = leaf("u2", unit="u1", ordinal=1)
    continuation[field] = "different"
    with pytest.raises(ValueError, match="STRUCTURE_UNAVAILABLE"):
        build_unit([root, continuation], tree_version_id="tree-1")


def test_b3_reuses_one_query_embedding_and_expands_admitted_unit(monkeypatch):
    from knowpath_backend.learning.rag.unit_first import UnitFirstPlugin

    embedder = Embedder()
    plugin = UnitFirstPlugin(embedder, Dense())
    rows = [leaf("u1", "alpha first", ordinal=0),
            leaf("u2", "alpha second", unit="u1", ordinal=1),
            leaf("other", "beta", ordinal=2)]
    units = {"u1": {"leaf_ids": ["u1", "u2"], "retrieval_text": "alpha first\nalpha second"}}

    def rankings(request, rows, *, query_vector=None, total_limit=None):
        assert query_vector is not None
        return {"keyword": [], "dense": [], "fused": [("u1", 1.0)],
                "query_vector": query_vector, "embedding_calls": 0,
                "bm25_cache_hit": False}

    monkeypatch.setattr(plugin, "_hybrid_rankings", rankings)
    result = plugin.retrieve(request(rerank_candidates=4, context_tokens=20), rows, units=units)

    assert [candidate["chunk_id"] for candidate in result["candidates"]] == ["u1", "u2"]
    assert result["trace"]["embedding_calls"] == 1
    assert result["trace"]["rerank_mode"] == "unit_atomic"


def test_b3_rejects_unit_with_missing_authorized_leaf():
    from knowpath_backend.learning.rag.unit_first import UnitFirstPlugin

    plugin = UnitFirstPlugin(Embedder(), Dense())
    rows = [leaf("u1", "alpha first", ordinal=0)]
    units = {"u1": {"leaf_ids": ["u1", "missing"], "retrieval_text": "alpha first"}}
    result = plugin.retrieve(request(rerank_candidates=4, context_tokens=20), rows, units=units)

    assert [candidate["chunk_id"] for candidate in result["candidates"]] == ["u1"]
    assert result["trace"]["fallback_reason"] == "UNIT_SOURCE_INVALID"


def test_runtime_accepts_b3_unit_mode_without_changing_default(build_workspace, monkeypatch):
    from knowpath_backend.learning.rag.runtime import configured_pipeline

    state, *_ = build_workspace
    monkeypatch.delenv("LEARNING_RAG_PLUGIN", raising=False)
    assert configured_pipeline(state.material_repository, state.space_service) is None
    monkeypatch.setenv("LEARNING_RAG_PLUGIN", "b3_unit")
    assert configured_pipeline(state.material_repository, state.space_service).mode == "b3_unit"


def test_atomic_rerank_groups_expand_in_group_order():
    from knowpath_backend.learning.rag.pipeline import expand_atomic_rerank_groups

    rows = {
        "u1": leaf("u1", "first", ordinal=0),
        "u2": leaf("u2", "second", unit="u1", ordinal=1),
        "other": leaf("other", "other", ordinal=2),
    }
    ranked_groups = [
        {"group_id": "u1", "leaf_ids": ["u1", "u2"]},
        {"group_id": "other", "leaf_ids": ["other"]},
    ]
    assert [row["chunk_id"] for row in expand_atomic_rerank_groups(ranked_groups, rows)] == [
        "u1", "u2", "other"
    ]
