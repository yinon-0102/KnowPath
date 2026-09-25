"""Coordinate-union ranking and B3 attribution must not manufacture evidence."""
import json

import pytest

from knowpath_backend.rag_eval import scoring


def source(identifier, start=0, end=10, **identity):
    return {"chunk_id": identifier, "source_spans": [{
        "material_version_id": "mv", "artifact_hash": "hash", "page": 1,
        "block": "block", "start": start, "end": end, **identity}]}


def union_metrics(items, required):
    calculate = getattr(scoring, "_union_rank_metrics", None)
    assert callable(calculate), "coordinate-union ranking is not implemented"
    return calculate(items, required)


def b3_response(candidates, *, baseline=None, expansions=None, reranked=None):
    retrieval = {"unit_hits": [], "unit_expansions": expansions or [],
                 "structural_additions": 0, "a_retention_at_40": 1.0}
    if baseline is not None:
        retrieval["baseline_candidate_ids"] = baseline
    return {"sources": candidates, "trace": {"plugin": "b3_unit",
        "retrieval": retrieval, "candidate_sources": candidates,
        "reranked_sources": candidates if reranked is None else reranked}}


def test_union_ranking_combines_adjacent_leaves_without_changing_single_leaf_ndcg():
    gold = [source("gold")["source_spans"][0]]
    candidates = [source("left", 0, 4), source("right", 4, 10)]
    old = scoring._rank_metrics(candidates, gold)
    metric = union_metrics(candidates, gold)
    assert metric["recall_at"] == {"1": 0.0, "3": 1.0, "5": 1.0,
                                   "10": 1.0, "20": 1.0, "40": 1.0}
    assert old["recall_at"]["3"] == old["ndcg_at"]["3"] == 0.0
    assert "ndcg_at" not in metric


@pytest.mark.parametrize("other", [
    source("gap", 5, 10), source("duplicate", 0, 4),
    source("version", 4, 10, material_version_id="other"),
    source("artifact", 4, 10, artifact_hash="other"),
    source("page", 4, 10, page=2), source("block", 4, 10, block="other"),
])
def test_union_ranking_rejects_gaps_duplicates_and_different_source_identity(other):
    metric = union_metrics([source("left", 0, 4), other],
                           [source("gold")["source_spans"][0]])
    assert set(metric["recall_at"].values()) == {0.0}


def test_union_ranking_counts_each_gold_once_and_honors_prefix_boundaries():
    gold = [source("gold")["source_spans"][0],
            source("second", 20, 30)["source_spans"][0]]
    candidates = [source("left", 0, 6), source("overlap", 4, 10),
                  source("duplicate", 0, 6), source("last", 20, 30)]
    metric = union_metrics(candidates, gold)
    assert metric["gold_spans"] == 2
    assert metric["list_length"] == 4
    assert metric["recall_at"]["3"] == 0.5
    assert metric["recall_at"]["5"] == 1.0
    assert union_metrics(None, gold) is None
    assert union_metrics(candidates, []) is None
    assert set(union_metrics([], gold)["recall_at"].values()) == {0.0}


def test_b3_evidence_already_covered_by_a_has_zero_increment():
    first, sibling = source("first", 20, 30), source("sibling")
    response = b3_response([first, sibling], baseline=["first", "sibling"],
        expansions=[{"anchor_id": "first", "leaf_ids": ["first", "sibling"]}])
    result = scoring._b3_attribution(response, sibling["source_spans"])
    assert result["added_evidence_recall"] == 0.0
    assert result["added_evidence_recall_status"] == "known"


def test_b3_increment_can_complete_gold_with_a_baseline_fragment():
    first, sibling = source("first", 0, 4), source("sibling", 4, 10)
    response = b3_response([first, sibling], baseline=["first"],
        expansions=[{"anchor_id": "first", "leaf_ids": ["first", "sibling"]}])
    result = scoring._b3_attribution(response, source("gold")["source_spans"])
    assert result["added_evidence_recall"] == 1.0


def test_b3_increment_uses_complete_pool_difference_including_new_anchor():
    baseline, anchor = source("baseline", 20, 30), source("new-anchor")
    response = b3_response([baseline, anchor], baseline=["baseline"],
        expansions=[{"anchor_id": "new-anchor", "leaf_ids": ["new-anchor"]}])
    assert scoring._b3_attribution(response, anchor["source_spans"])["added_evidence_recall"] == 1.0


@pytest.mark.parametrize("baseline,status", [
    (None, "missing_baseline"), (["absent"], "incomplete_baseline"),
])
def test_b3_unrecoverable_baseline_is_unknown(baseline, status):
    first, added = source("first", 20, 30), source("added")
    response = b3_response([first, added], baseline=baseline,
        expansions=[{"anchor_id": "first", "leaf_ids": ["first", "added"]}])
    result = scoring._b3_attribution(response, added["source_spans"])
    assert result["added_evidence_recall"] is None
    assert result["added_evidence_recall_status"] == status


def test_b3_baseline_sources_recover_evicted_a_evidence():
    anchor, added, evicted = source("anchor", 20, 30), source("added"), source("evicted")
    response = b3_response([anchor, added], baseline=["evicted"],
        expansions=[{"anchor_id": "anchor", "leaf_ids": ["anchor", "added"]}])
    response["trace"]["retrieval"]["baseline_candidate_sources"] = [evicted]
    result = scoring._b3_attribution(response, added["source_spans"])
    assert result["added_evidence_recall"] == 0.0
    assert result["added_evidence_recall_status"] == "known"


def test_b3_can_resolve_baseline_ids_from_reranked_sources():
    anchor, added, evicted = source("anchor", 20, 30), source("added"), source("evicted")
    response = b3_response([anchor, added], baseline=["evicted"],
        reranked=[evicted, anchor, added],
        expansions=[{"anchor_id": "anchor", "leaf_ids": ["anchor", "added"]}])
    result = scoring._b3_attribution(response, added["source_spans"])
    assert result["added_evidence_recall"] == 0.0


def test_b3_increment_uses_full_a_baseline_even_when_its_fragment_was_evicted():
    first, sibling = source("first", 0, 4), source("sibling", 4, 10)
    response = b3_response([sibling], baseline=["first"],
        expansions=[{"anchor_id": "sibling", "leaf_ids": ["sibling"]}])
    response["trace"]["retrieval"]["baseline_candidate_sources"] = [first]
    assert scoring._b3_attribution(response, source("gold")["source_spans"])["added_evidence_recall"] == 1.0
    assert scoring.evidence_coverage(response, {"necessary_evidence": source("gold")["source_spans"]}, "sources") == 0.0


def test_b3_singletons_and_duplicate_leaf_ids_are_not_expansions():
    response = b3_response([source("single"), source("anchor"), source("leaf")],
        baseline=[], expansions=[
            {"anchor_id": "single", "leaf_ids": ["single", "single"]},
            {"anchor_id": "anchor", "leaf_ids": ["anchor", "leaf"]},
            {"anchor_id": "anchor", "leaf_ids": ["anchor", "leaf"]},
        ])
    result = scoring._b3_attribution(response, source("gold")["source_spans"])
    assert result["unit_expansion_count"] == 1
    assert result["unit_expansion_record_count"] == 3


def test_report_aggregates_union_rankings_and_unknown_b3_by_domain_and_relation(tmp_path, monkeypatch):
    rows = [dict(question_id=f"q{i}", family_id=f"f{i}", source_domain=f"d{i}",
                 relation_type=f"r{i}", categories=[f"c{i}"],
                 necessary_evidence=source("gold")["source_spans"]) for i in range(2)]
    frozen = {"freeze_id": "freeze", "config": {"modes": ["a", "b3_unit"], "repeats": 1,
        "gates": {name: None for name in scoring.GATES}}}
    split = {"dev": {"question_ids": [row["question_id"] for row in rows]}}
    monkeypatch.setattr(scoring, "verify_freeze", lambda path: (frozen, rows, split))
    records = []
    for i, row in enumerate(rows):
        candidates = [source("left", 0, 4), source("right", 4, 10)] if i == 0 else []
        for mode in ("a", "b3_unit"):
            response = b3_response(candidates, baseline=None if i == 0 else [])
            response["trace"]["plugin"] = mode
            if mode == "a":
                response["trace"]["retrieval"] = {}
            records.append(dict(question_id=row["question_id"], plugin=mode, repeat=0,
                freeze_id="freeze", partition="dev", service_success=True,
                latency_ms=1, cost=None, response=response))
    results = tmp_path / "results.jsonl"
    content = "\n".join(json.dumps(row) for row in records)
    results.write_text(content, encoding="utf-8")
    result = scoring.report(tmp_path / "freeze.json", results)
    for mode in ("a", "b3_unit"):
        for field in ("candidate_union_ranking", "reranked_union_ranking"):
            assert result["plugins"][mode].get(field, {}).get("recall_at", {}).get("3") == 0.5
            for section, prefix in (("domains", "d"), ("relations", "r"), ("categories", "c")):
                assert result[section][f"{prefix}0"][mode][field]["recall_at"]["3"] == 1.0
                assert result[section][f"{prefix}1"][mode][field]["recall_at"]["3"] == 0.0
        assert result["plugins"][mode]["candidate_ranking"]["recall_at"]["3"] == 0.0
    b3 = result["plugins"]["b3_unit"]["b3_attribution"]
    assert b3["added_evidence_recall"] == 0.0
    assert b3["added_evidence_recall_denominator"] == 1
    assert b3["added_evidence_recall_unknown"] == 1
    assert result["domains"]["d0"]["b3_unit"]["b3_attribution"]["added_evidence_recall"] is None
    assert "candidate_union_ranking" in result["metric_definitions"]
    assert "single" in result["metric_definitions"]["candidate_ranking"].lower()
    assert results.read_text(encoding="utf-8") == content
