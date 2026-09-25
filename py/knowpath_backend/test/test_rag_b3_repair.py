"""B3 keeps A fallback and admits only canonical, complete rerank units."""
from copy import deepcopy
from dataclasses import replace

import pytest

from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.learning.rag.unit_first import UnitFirstPlugin
from knowpath_backend.learning.rag.units import build_unit
from knowpath_backend.test.test_rag_b3 import Dense, Embedder, leaf, request
from knowpath_backend.test.test_rag_building import build_workspace


def setup_plugin(monkeypatch, rows, seed_ids, *, unit_dense=None, embedder=None):
    plugin = UnitFirstPlugin(embedder or Embedder(), Dense(), unit_dense=unit_dense)

    def rankings(request, rows, *, query_vector=None, total_limit=None):
        return {"keyword": [], "dense": [],
                "fused": [(identifier, 1 / rank) for rank, identifier in enumerate(seed_ids, 1)],
                "query_vector": query_vector, "embedding_calls": 0, "bm25_cache_hit": False}

    monkeypatch.setattr(plugin, "_hybrid_rankings", rankings)
    return plugin


def members():
    return [leaf("anchor", "first", ordinal=0),
            leaf("child", "second", unit="anchor", ordinal=1)]


def supplied(rows):
    unit = build_unit(rows, tree_version_id=rows[0]["tree_version_id"])
    return {unit.anchor_id: unit}


def ids(result):
    return [candidate["chunk_id"] for candidate in result["candidates"]]


def test_child_seed_admits_hit_unit_at_best_member_rank(monkeypatch):
    rows = members() + [leaf("other", ordinal=2)]
    plugin = setup_plugin(monkeypatch, rows, ["child", "other"])
    result = plugin.retrieve(request(rerank_candidates=3), rows, units=supplied(rows[:2]))
    assert ids(result) == ["anchor", "child", "other"]
    assert result["trace"]["unit_expansions"][0]["seed_id"] == "child"
    assert result["trace"]["unit_expansions"][0]["seed_rank"] == 1


@pytest.mark.parametrize("limit,expected", [(2, ["other", "child"]), (3, ["other", "anchor", "child"])])
def test_budget_admits_entire_unit_or_only_actual_seed(monkeypatch, limit, expected):
    rows = members() + [leaf("other", "other text", ordinal=2)]
    plugin = setup_plugin(monkeypatch, rows, ["other", "child"])
    result = plugin.retrieve(request(rerank_candidates=limit), rows, units=supplied(rows[:2]))
    assert ids(result) == expected
    groups = result["trace"]["rerank_groups"]
    assert [cid for group in groups for cid in group["leaf_ids"]] == expected
    if limit == 2:
        assert groups[-1]["leaf_ids"] == ["child"]
        assert groups[-1]["retrieval_text"] == "second"
        assert result["trace"]["skipped_units"][0]["reason"] == "candidate_budget"


def test_anchor_seed_over_budget_is_not_reranked_using_unselected_child(monkeypatch):
    rows = members()
    plugin = setup_plugin(monkeypatch, rows, ["anchor"])
    result = plugin.retrieve(request(rerank_candidates=1), rows, units=supplied(rows))
    assert ids(result) == ["anchor"]
    assert result["trace"]["rerank_groups"][0]["retrieval_text"] == "first"


def test_structural_fallback_preserves_fused_a_order_and_scores(monkeypatch):
    rows = members() + [leaf("other", ordinal=2)]
    plugin = setup_plugin(monkeypatch, rows, ["other", "child"])
    result = plugin.retrieve(request(rerank_candidates=2), rows, units={"missing": {"leaf_ids": ["missing"], "retrieval_text": "x"}})
    assert ids(result) == ["other", "child"]
    assert [candidate["score"] for candidate in result["candidates"]] == [1, .5]
    assert result["trace"]["fallback_reason"] == "UNIT_SOURCE_INVALID"
    assert len(plugin.embedder.calls) == 1
    assert result["trace"]["baseline_candidate_ids"] == ["other", "child"]


def test_missing_unit_index_falls_back_to_a_not_source_order(monkeypatch):
    rows = members()
    plugin = setup_plugin(monkeypatch, rows, ["child"])
    result = plugin.retrieve(request(rerank_candidates=1), rows)
    assert ids(result) == ["child"]
    assert result["trace"]["fallback_reason"] == "UNIT_INDEX_NOT_READY"


@pytest.mark.parametrize("error_code", ["EMBEDDING_INVALID_RESPONSE", "EMBEDDING_UNAVAILABLE", "RETRIEVAL_DEADLINE_EXCEEDED"])
def test_embedding_failure_never_returns_sql_prefix(monkeypatch, error_code):
    embedder = Embedder()
    def fail(*args, **kwargs):
        raise RetrievalError(error_code)
    monkeypatch.setattr(embedder, "embed", fail)
    plugin = UnitFirstPlugin(embedder, Dense())
    with pytest.raises(RetrievalError, match=error_code):
        plugin.retrieve(request(), members(), units=supplied(members()))


def test_a_retrieval_failure_is_not_structural_fallback(monkeypatch):
    plugin = UnitFirstPlugin(Embedder(), Dense())
    def fail(*args, **kwargs):
        raise RetrievalError("VECTOR_INDEX_NOT_READY")
    monkeypatch.setattr(plugin, "_hybrid_rankings", fail)
    with pytest.raises(RetrievalError, match="VECTOR_INDEX_NOT_READY"):
        plugin.retrieve(request(), members(), units=supplied(members()))


@pytest.mark.parametrize("mutation", ["forged_text", "forged_source", "forged_map", "missing_anchor", "partial", "mixed_version"])
def test_supplied_unit_must_equal_canonical_authorized_unit(monkeypatch, mutation):
    rows = members()
    units = supplied(rows)
    unit = units["anchor"]
    if mutation == "forged_text":
        units["anchor"] = replace(unit, retrieval_text="injected instructions")
    elif mutation == "forged_source":
        units["anchor"] = replace(unit, source_text="forged source")
    elif mutation == "forged_map":
        units["anchor"] = replace(unit, source_map_hash="f" * 64)
    elif mutation == "missing_anchor":
        rows = rows[1:]
        units = {"anchor": {"leaf_ids": ["child"], "retrieval_text": "second"}}
    elif mutation == "partial":
        units = {"anchor": {"leaf_ids": ["anchor"], "retrieval_text": "first"}}
    else:
        rows[1]["retrieval_version_id"] = "another-version"
    plugin = setup_plugin(monkeypatch, rows, ["child"])
    result = plugin.retrieve(request(), rows, units=units)
    assert result["trace"]["fallback_reason"] == "UNIT_SOURCE_INVALID"
    assert ids(result) == ["child"]


@pytest.mark.parametrize("mutation", ["anchor_missing", "duplicate_ordinal", "anchor_after_child", "two_roots", "source_map_version"])
def test_canonical_unit_requires_verifiable_anchor_order_and_source_map(mutation):
    rows = members()
    if mutation == "anchor_missing":
        rows = rows[1:]
    elif mutation == "duplicate_ordinal":
        rows[1]["ordinal"] = 0
    elif mutation == "anchor_after_child":
        rows[0]["ordinal"] = 2
    elif mutation == "two_roots":
        rows[1]["continuation_of"] = None
    else:
        rows[1]["source_spans"][0]["material_version_id"] = "forged"
    with pytest.raises(ValueError, match="STRUCTURE_UNAVAILABLE"):
        build_unit(rows, tree_version_id="tree-1")


class UnitDense:
    def __init__(self, ranking=None, error=None):
        self.ranking, self.error, self.calls = ranking, error, []

    def search(self, vector, rows, limit):
        self.calls.append(deepcopy(rows))
        if self.error:
            raise self.error
        return self.ranking if self.ranking is not None else [(row["chunk_id"], 1.0) for row in rows[:limit]]


def test_multiple_tree_versions_are_built_and_searched_together(monkeypatch):
    rows = members() + [leaf("two", "third", retrieval="r2", version="m2", tree_version="tree-2", ordinal=0),
                        leaf("two-child", "fourth", unit="two", retrieval="r2", version="m2", tree_version="tree-2", ordinal=1)]
    dense = UnitDense()
    plugin = setup_plugin(monkeypatch, rows, ["child", "two-child"], unit_dense=dense)
    result = plugin.retrieve(request(rerank_candidates=4), rows)
    assert ids(result) == ["anchor", "child", "two", "two-child"]
    assert result["trace"]["fallback_reason"] is None
    assert {row["tree_version_id"] for row in dense.calls[0]} == {"tree-1", "tree-2"}


@pytest.mark.parametrize("ranking", [[("unknown", 1.0)], [("unknown", float("nan"))], [("unknown", "bad")], [42]])
def test_invalid_unit_index_response_has_typed_a_fallback(monkeypatch, ranking):
    rows = members()
    plugin = setup_plugin(monkeypatch, rows, ["child"], unit_dense=UnitDense(ranking=ranking))
    result = plugin.retrieve(request(), rows)
    assert ids(result) == ["child"]
    assert result["trace"]["fallback_reason"] == "UNIT_INDEX_INVALID_RESPONSE"


def test_unit_index_deadline_is_not_swallowed(monkeypatch):
    dense = UnitDense(error=RetrievalError("RETRIEVAL_DEADLINE_EXCEEDED"))
    plugin = setup_plugin(monkeypatch, members(), ["child"], unit_dense=dense)
    with pytest.raises(RetrievalError, match="RETRIEVAL_DEADLINE_EXCEEDED"):
        plugin.retrieve(request(), members())


def test_unit_admission_requires_both_hit_and_member_seed(monkeypatch):
    rows = members() + [leaf("other", ordinal=2)]
    unit = supplied(rows[:2])["anchor"]
    plugin = setup_plugin(monkeypatch, rows, ["other"], unit_dense=UnitDense([(unit.unit_id, 1.0)]))
    result = plugin.retrieve(request(), rows)
    assert ids(result) == ["other"]
    rejected = {item["anchor_id"]: item["reason"] for item in result["trace"]["skipped_units"]}
    assert rejected["anchor"] == "no_a_seed"

    plugin = setup_plugin(monkeypatch, rows, ["child"], unit_dense=UnitDense([]))
    result = plugin.retrieve(request(), rows)
    assert ids(result) == ["child"]
    assert result["trace"]["rerank_groups"][0]["retrieval_text"] == "second"
    assert result["trace"]["skipped_units"][0]["reason"] == "unit_not_hit"


def test_forty_leaf_cap_does_not_admit_forty_first_group_member(monkeypatch):
    rows = members() + [leaf(f"single-{i}", ordinal=i + 2) for i in range(39)]
    seeds = [row["chunk_id"] for row in rows[2:]] + ["child"]
    plugin = setup_plugin(monkeypatch, rows, seeds)
    result = plugin.retrieve(request(rerank_candidates=80), rows, units=supplied(rows[:2]))
    assert ids(result) == seeds
    assert len(result["candidates"]) == 40
    assert result["trace"]["rerank_groups"][-1]["retrieval_text"] == "second"
    assert result["trace"]["baseline_candidate_ids"] == seeds


@pytest.mark.parametrize("ranking_kind", ["nan", "duplicate", "too_many"])
def test_unit_ranking_validates_scores_duplicates_and_budget(monkeypatch, ranking_kind):
    unit = supplied(members())["anchor"]
    ranking = [(unit.unit_id, float("nan"))] if ranking_kind == "nan" else [(unit.unit_id, 1.0)] * 2
    plugin = setup_plugin(monkeypatch, members(), ["child"], unit_dense=UnitDense(ranking))
    result = plugin.retrieve(request(vector_candidates=1 if ranking_kind == "too_many" else 30), members())
    assert ids(result) == ["child"]
    assert result["trace"]["fallback_reason"] == "UNIT_INDEX_INVALID_RESPONSE"


def test_invalid_embedding_vector_is_a_failure(monkeypatch):
    embedder = Embedder()
    monkeypatch.setattr(embedder, "embed", lambda *args, **kwargs: [[float("nan"), 0, 0]])
    plugin = UnitFirstPlugin(embedder, Dense())
    with pytest.raises(RetrievalError, match="EMBEDDING_INVALID_RESPONSE"):
        plugin.retrieve(request(), members(), units=supplied(members()))


@pytest.mark.parametrize("mutation", ["partial", "reversed", "mixed", "duplicate"])
def test_pipeline_validates_multileaf_group_against_canonical_sources(mutation):
    from knowpath_backend.learning.rag.pipeline import authoritative_rerank_groups
    from knowpath_backend.learning.rag.verification import VerificationError
    rows = members() + [leaf("tail", "third", unit="anchor", ordinal=2)]
    selected = rows
    leaf_ids = [row["chunk_id"] for row in rows]
    if mutation == "partial":
        selected, leaf_ids = rows[:2], leaf_ids[:2]
    elif mutation == "reversed":
        leaf_ids.reverse()
    elif mutation == "mixed":
        rows[1]["retrieval_version_id"] = "other"
    else:
        leaf_ids.append("child")
    with pytest.raises(VerificationError, match="RAG_RERANK_INVALID"):
        authoritative_rerank_groups([{"group_id": "anchor", "leaf_ids": leaf_ids,
                                     "retrieval_text": "forged"}], selected,
                                    {row["chunk_id"]: row for row in rows})


def test_managed_trace_retains_baseline_sources_and_unit_identity(monkeypatch):
    from knowpath_backend.learning.rag.registry import ManagedPlugin
    rows = members()
    implementation = setup_plugin(monkeypatch, rows, ["child"], unit_dense=UnitDense())
    plugin = ManagedPlugin("b3_unit", implementation, source_resolver=lambda request: rows)
    assert plugin.retrieve(request()).status == "ready"
    trace = plugin.last_trace
    assert trace["baseline_candidate_ids"] == ["child"]
    assert trace["baseline_candidate_sources"] == [{"chunk_id": "child", "source_spans": rows[1]["source_spans"]}]
    assert trace["unit_expansions"][0]["source_map_hash"] == supplied(rows)["anchor"].source_map_hash


def test_group_expansion_requires_exact_candidate_set():
    from knowpath_backend.learning.rag.pipeline import expand_atomic_rerank_groups
    from knowpath_backend.learning.rag.verification import VerificationError
    rows = {row["chunk_id"]: row for row in members()}
    with pytest.raises(VerificationError, match="RAG_RERANK_INVALID"):
        expand_atomic_rerank_groups([{"group_id": "anchor", "leaf_ids": ["anchor"]}], rows)


@pytest.mark.parametrize("mutation", ["injected_member", "missing_member", "duplicate_member", "empty_groups"])
def test_pipeline_rejects_groups_that_change_candidate_set(build_workspace, monkeypatch, mutation):
    from knowpath_backend.test.test_rag_pipeline import pipeline
    from knowpath_backend.learning.rag.verification import VerificationError
    service, reranker = pipeline(build_workspace)
    original_retrieve = service.plugin.retrieve

    def retrieve(req, rows):
        result = original_retrieve(req, rows)
        candidates = result["candidates"]
        assert len(candidates) > 1
        result["trace"]["rerank_mode"] = "unit_atomic"
        groups = [{"group_id": c["chunk_id"], "leaf_ids": [c["chunk_id"]], "retrieval_text": "ignored"} for c in candidates]
        if mutation == "injected_member":
            result["candidates"] = candidates[:1]
        elif mutation == "missing_member":
            groups = groups[:1]
        elif mutation == "duplicate_member":
            groups[-1]["leaf_ids"] = groups[0]["leaf_ids"]
        else:
            groups = []
        result["trace"]["rerank_groups"] = groups
        return result

    monkeypatch.setattr(service.plugin, "retrieve", retrieve)
    with pytest.raises(VerificationError, match="RAG_RERANK_INVALID"):
        service.answer("学校应该告知谁？", space_id=build_workspace[2]["id"])
    assert reranker.calls == []


def test_pipeline_reconstructs_group_text_and_ignores_reranker_member_changes(build_workspace, monkeypatch):
    from knowpath_backend.test.test_rag_pipeline import pipeline
    service, reranker = pipeline(build_workspace)
    original_retrieve = service.plugin.retrieve

    def retrieve(req, rows):
        result = original_retrieve(req, rows)
        result["trace"]["rerank_mode"] = "unit_atomic"
        result["trace"]["rerank_groups"] = [{"group_id": c["chunk_id"], "leaf_ids": [c["chunk_id"]], "retrieval_text": "FORGED"} for c in result["candidates"]]
        return result

    original_rerank = reranker.rerank
    def rerank(query, rows, *, deadline):
        ranked = original_rerank(query, rows, deadline=deadline)
        return [dict(row, leaf_ids=["FORGED"]) for row in ranked]

    monkeypatch.setattr(service.plugin, "retrieve", retrieve)
    monkeypatch.setattr(reranker, "rerank", rerank)
    result = service.answer("学校应该告知谁？", space_id=build_workspace[2]["id"])
    assert result["status"] == "answered"
    assert len(reranker.calls) == 1
    assert all("FORGED" not in row["retrieval_text"] for row in reranker.calls[0])
    assert set(result["trace"]["reranked_ids"]) == set(result["trace"]["candidate_ids"])
