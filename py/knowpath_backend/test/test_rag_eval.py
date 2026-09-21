"""Frozen paired evaluation excludes gold and never hides failed attempts."""
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest


def _rank_source(chunk_id, start=0, end=10):
    return {"chunk_id": chunk_id, "source_spans": [{"material_version_id": "mv",
        "artifact_hash": "hash", "page": 1, "block": "block", "start": start, "end": end}]}


def test_tree_ranking_metrics_score_multi_gold_and_no_hit():
    from knowpath_backend.rag_eval.scoring import _rank_metrics
    gold = [{"material_version_id": "mv", "artifact_hash": "hash", "page": 1,
             "block": "block", "start": 0, "end": 10},
            {"material_version_id": "mv", "artifact_hash": "hash", "page": 1,
             "block": "block", "start": 20, "end": 30}]
    metrics = _rank_metrics([_rank_source("noise", 100, 110), _rank_source("first"),
                             _rank_source("second", 20, 30)], gold)
    assert metrics["recall_at"]["1"] == 0.0
    assert metrics["recall_at"]["3"] == 1.0
    assert metrics["mrr"] == 0.5
    assert _rank_metrics([], gold)["mrr"] == 0.0


def test_tree_attribution_counts_only_extension_hits():
    from knowpath_backend.rag_eval.scoring import _tree_attribution
    gold = [{"material_version_id": "mv", "artifact_hash": "hash", "page": 1,
             "block": "block", "start": 0, "end": 10}]
    response = {"trace": {"retrieval": {"extensions": [{"chunk_id": "ext"}],
        }, "candidate_sources": [_rank_source("ext"), _rank_source("noise")]}}
    result = _tree_attribution(response, gold)
    assert result["extension_count"] == 1
    assert result["extension_hit_count"] == 1
    assert result["extension_precision"] == 1.0
    assert result["extension_only_recall"] == 1.0


@pytest.fixture
def evaluation(tmp_path):
    root = Path(__file__).resolve().parents[3] / "docs/research/tree-rag/evaluation"
    configuration = dict(dataset=str(root / "dataset.jsonl"), split=str(root / "split.json"),
        rubric=str(root / "rubric.md"), human_review_status="pending", repeats=2,
        order_schedule="alternating", retries=0, profile={"fixture": "v1"},
        models={"chat_model": "fixture", "embedding_model": "fixture"},
        prompts={"version": "fixture-v1"}, budgets={"context_tokens": 5000},
        statistics={"method": "family_descriptive", "estimand": "full_task_success_difference",
            "weighting": "equal_family", "repeat_aggregation": "question_mean", "failures": "zero",
            "quantile": "nearest_rank", "seed": 0},
        gates={key: None for key in ("max_p95_latency_ms", "max_mean_cost_per_request", "max_b1_latency_ratio", "max_b1_cost_ratio")},
        pricing={"currency": "USD", "date": None, "price_table": None}, decision_rule="exploratory_no_adoption",
        runtime_bindings={})
    profile_hash = sha256(json.dumps(configuration["profile"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for row in map(json.loads, (root / "dataset.jsonl").read_text().splitlines()):
        configuration["runtime_bindings"][row["scope_snapshot_id"]] = dict(space_id="real-space", scope_snapshot_id="real-scope",
            expected_scope_version=1, expected_bindings=[dict(material_id="material", material_version_id="mv", graph_version=1)],
            expected_manifest_ids=["manifest"], expected_retrieval_versions=["rv"],
            manifest_configuration_hashes={"manifest": profile_hash})
    config = tmp_path / "config.json"
    config.write_text(json.dumps(configuration), encoding="utf-8")
    return config, configuration


def frozen(evaluation):
    from knowpath_backend.rag_eval.dataset import freeze
    path, _ = evaluation
    destination = path.parent / "freeze.json"
    freeze(path, destination)
    return destination


def resolver(space_id):
    return dict(space_id=space_id, scope_snapshot_id="real-scope", scope_version=1,
        bindings=[dict(material_id="material", material_version_id="mv", graph_version=1)],
        manifests=[dict(manifest_id="manifest", retrieval_version_id="rv", configuration={"fixture": "v1"})])


def answer():
    return dict(status="answered", answer="model answer", trace={"scope_snapshot_id": "real-scope",
        "manifest_ids": ["manifest"], "retrieval_versions": ["rv"], "self_check_success": True})


def test_freeze_detects_configuration_and_source_tampering(evaluation, tmp_path):
    from knowpath_backend.rag_eval.dataset import freeze, verify_freeze
    path = frozen(evaluation)
    verify_freeze(path)
    config, configuration = evaluation
    configuration["repeats"] = 3
    config.write_text(json.dumps(configuration))
    with pytest.raises(ValueError, match="hash"):
        verify_freeze(path)


def test_family_leakage_is_rejected(evaluation):
    from knowpath_backend.rag_eval.dataset import freeze
    config, configuration = evaluation
    split = json.loads(Path(configuration["split"]).read_text())
    split["heldout"]["family_ids"].append(split["dev"]["family_ids"][0])
    target = config.parent / "split.json"
    target.write_text(json.dumps(split))
    configuration["split"] = str(target)
    config.write_text(json.dumps(configuration))
    with pytest.raises(ValueError, match="family"):
        freeze(config, config.parent / "freeze.json")


def test_heldout_requires_human_review_and_numeric_cost_gates(evaluation):
    from knowpath_backend.rag_eval.runner import run
    with pytest.raises(ValueError, match="heldout"):
        run(frozen(evaluation), evaluation[0].parent / "runs.jsonl", partition="heldout",
            pipeline_factory=lambda mode: None, scope_resolver=resolver)


def test_runner_only_passes_question_and_scope_alternates_and_keeps_failures(evaluation):
    from knowpath_backend.rag_eval.runner import run
    calls = []
    class Pipeline:
        def __init__(self, mode): self.mode = mode
        def answer(self, question, **kwargs):
            calls.append((self.mode, question, kwargs))
            assert set(kwargs) == {"space_id", "expected_scope_version", "expected_bindings", "history"}
            assert "required_answer_points" not in json.dumps(kwargs)
            if self.mode == "b1":
                raise RuntimeError("provider secret")
            return answer()
    records = run(frozen(evaluation), evaluation[0].parent / "runs.jsonl", pipeline_factory=Pipeline, scope_resolver=resolver)
    assert len(records) == 7 * 2 * 2
    assert [c[0] for c in calls[:4]] == ["a", "b1", "b1", "a"]
    assert sum(r["service_success"] for r in records) == 14
    assert all(r["error_code"] == "EVALUATION_REQUEST_FAILED" for r in records if not r["service_success"])
    assert "provider secret" not in (evaluation[0].parent / "runs.jsonl").read_text()
    assert all(r["cost"] is None for r in records)
    followup = next(c for c in calls if c[1] == "How long does it take?")
    assert followup[2]["history"] == [{"role": "user", "content": "I need to arrange registration and calibration."}]
    assert all(set(item) == {"role", "content"} for _, _, kwargs in calls for item in kwargs["history"])


def test_scoring_keeps_missing_reviews_unknown_and_partial_not_full(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    freeze_path = frozen(evaluation)
    output = evaluation[0].parent / "runs.jsonl"
    run(freeze_path, output, pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: answer()), scope_resolver=resolver)
    unknown = report(freeze_path, output)
    assert unknown["plugins"]["a"]["success_rate"] is None
    assert unknown["plugins"]["a"]["mean_cost_per_request"] is None
    assert unknown["gates"]["passed"] is None
    records = [json.loads(line) for line in output.read_text().splitlines()]
    reviews = [dict(question_id=r["question_id"], plugin=r["plugin"], repeat=r["repeat"],
                    success=r["plugin"] == "a", partial=r["plugin"] == "b1", error_type="context") for r in records]
    review_path = output.parent / "reviews.json"
    review_path.write_text(json.dumps(reviews))
    result = report(freeze_path, output, review_path)
    assert result["plugins"]["a"]["success_rate"] == 1
    assert result["plugins"]["b1"]["success_rate"] == 0
    assert result["plugins"]["b1"]["partial_rate"] == 1
    assert result["paired"]["a_wins"] == 7
    assert result["paired"]["independent_families"] == 5


def test_missing_execution_rows_remain_in_full_denominator(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    freeze_path = frozen(evaluation)
    output = evaluation[0].parent / "runs.jsonl"
    run(freeze_path, output, pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: answer()), scope_resolver=resolver)
    lines = output.read_text().splitlines()
    output.write_text("\n".join(lines[:1]) + "\n")
    result = report(freeze_path, output)
    assert result["plugins"]["a"]["denominator"] == 14
    assert result["plugins"]["b1"]["denominator"] == 14
    assert result["missing_execution_rows"] == 27


def test_runtime_snapshot_change_becomes_failure_without_retry(evaluation):
    from knowpath_backend.rag_eval.runner import run
    result = answer()
    result["trace"]["manifest_ids"] = ["changed"]
    rows = run(frozen(evaluation), evaluation[0].parent / "runs.jsonl",
        pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: result), scope_resolver=resolver)
    assert len(rows) == 28 and not any(row["service_success"] for row in rows)


def test_frozen_source_bytes_cannot_change(evaluation):
    import shutil
    from knowpath_backend.rag_eval.dataset import verify_freeze
    config, configuration = evaluation
    copied = config.parent / "data"
    shutil.copytree(Path(configuration["dataset"]).parent, copied)
    for name, filename in (("dataset", "dataset.jsonl"), ("split", "split.json"), ("rubric", "rubric.md")):
        configuration[name] = str(copied / filename)
    config.write_text(json.dumps(configuration))
    freeze_path = frozen(evaluation)
    source = copied / "sources/dev_permit.txt"
    source.write_bytes(source.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash"):
        verify_freeze(freeze_path)


def test_approved_heldout_still_rejects_missing_numeric_gates(evaluation):
    import shutil
    from knowpath_backend.rag_eval.runner import run
    config, configuration = evaluation
    copied = config.parent / "data"
    shutil.copytree(Path(configuration["dataset"]).parent, copied)
    for name, filename in (("dataset", "dataset.jsonl"), ("split", "split.json"), ("rubric", "rubric.md")):
        configuration[name] = str(copied / filename)
    rows = [dict(json.loads(line), human_review_status="approved") for line in (copied / "dataset.jsonl").read_text().splitlines()]
    (copied / "dataset.jsonl").write_text("\n".join(map(json.dumps, rows)))
    split = json.loads((copied / "split.json").read_text())
    split["human_review_status"] = "approved"
    (copied / "split.json").write_text(json.dumps(split))
    configuration["human_review_status"] = "approved"
    config.write_text(json.dumps(configuration))
    with pytest.raises(ValueError, match="numeric"):
        run(frozen(evaluation), config.parent / "runs.jsonl", partition="heldout",
            pipeline_factory=lambda mode: None, scope_resolver=resolver)


def test_runtime_models_and_budgets_enforced_before_opening_database(evaluation):
    from knowpath_backend.rag_eval.cli import configured_runtime
    from knowpath_backend.rag_eval.dataset import verify_freeze
    value, _, _ = verify_freeze(frozen(evaluation))
    with pytest.raises(ValueError, match="models/prompts/budgets"):
        configured_runtime(value)


def test_failed_calls_keep_full_pair_and_service_subset_denominators(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    path = frozen(evaluation)
    output = evaluation[0].parent / "runs.jsonl"
    class Pipeline:
        def __init__(self, mode): self.mode = mode
        def answer(self, *args, **kwargs):
            if self.mode == "b1": raise RuntimeError("unavailable")
            return answer()
    rows = run(path, output, pipeline_factory=Pipeline, scope_resolver=resolver)
    reviews = [dict(question_id=r["question_id"], plugin=r["plugin"], repeat=r["repeat"],
                    success=True, partial=False, error_type="none") for r in rows if r["service_success"]]
    review_path = output.parent / "review.json"
    review_path.write_text(json.dumps(reviews))
    result = report(path, output, review_path)
    assert result["plugins"]["b1"]["denominator"] == 14
    assert result["plugins"]["b1"]["service_success_denominator"] == 0
    assert result["plugins"]["b1"]["success_rate"] == 0
    assert result["plugins"]["b1"]["service_subset_success_rate"] is None
    assert result["paired"]["a_wins"] == 7


def test_report_does_not_accept_partial_as_full_human_success(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    path = frozen(evaluation)
    output = evaluation[0].parent / "runs.jsonl"
    partial = dict(answer(), status="partial")
    rows = run(path, output, pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: partial), scope_resolver=resolver)
    reviews = [dict(question_id=r["question_id"], plugin=r["plugin"], repeat=r["repeat"],
                    success=True, partial=False, error_type="none") for r in rows]
    review_path = output.parent / "review.json"
    review_path.write_text(json.dumps(reviews))
    result = report(path, output, review_path)
    assert result["plugins"]["a"]["success_rate"] == 0
    assert result["plugins"]["a"]["partial_rate"] == 1


def test_cli_freeze_and_heldout_rejection_need_no_network(evaluation, capsys):
    from knowpath_backend.rag_eval.cli import main
    config, _ = evaluation
    frozen_path = config.parent / "cli.freeze.json"
    assert main(["freeze", "--config", str(config), "--output", str(frozen_path)]) == 0
    assert "freeze_id" in capsys.readouterr().out
    assert main(["run", "--freeze", str(frozen_path), "--partition", "heldout",
                 "--output", str(config.parent / "heldout.jsonl")]) == 2
    assert not (config.parent / "heldout.jsonl").exists()


def test_runtime_scope_and_manifest_hash_mismatch_blocks_model_calls(evaluation):
    from knowpath_backend.rag_eval.runner import run
    called = []
    def factory(mode):
        called.append(mode)
        return SimpleNamespace(answer=lambda *args, **kwargs: answer())
    def wrong(space_id):
        current = resolver(space_id)
        current["manifests"][0]["configuration"] = {"fixture": "changed"}
        return current
    rows = run(frozen(evaluation), evaluation[0].parent / "runs.jsonl", pipeline_factory=factory, scope_resolver=wrong)
    assert not called and len(rows) == 28


def test_offline_evidence_coverage_requires_complete_original_span_identity():
    from knowpath_backend.rag_eval.scoring import evidence_coverage
    span = dict(material_version_id="mv", artifact_hash="a" * 64, page=1, block="source", start=0, end=10)
    gold = dict(necessary_evidence=[span, dict(span, start=20, end=30)])
    result = dict(sources=[dict(source_spans=[dict(span, end=5), dict(span, start=5)])])
    assert evidence_coverage(result, gold, "sources") == .5
    result["sources"][0]["source_spans"][1]["artifact_hash"] = "b" * 64
    assert evidence_coverage(result, gold, "sources") == 0
    assert evidence_coverage(result, {"necessary_evidence": []}, "sources") is None


def test_candidate_and_rerank_coverage_use_offline_trace_with_missing_separate():
    from knowpath_backend.rag_eval.scoring import evidence_coverage
    span = dict(material_version_id='mv', artifact_hash='a'*64, page=1, block='s', start=0, end=10)
    gold = {'necessary_evidence':[span]}
    response = {'trace':{'candidate_sources':[{'source_spans':[span]}], 'reranked_sources':[]}}
    assert evidence_coverage(response,gold,'candidate_sources') == 1
    assert evidence_coverage(response,gold,'reranked_sources') == 0
    assert evidence_coverage({},gold,'candidate_sources') is None
