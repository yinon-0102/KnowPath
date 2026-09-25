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


def test_tree_ranking_ndcg_uses_fixed_gold_ideal_not_retrieved_hits():
    from knowpath_backend.rag_eval.scoring import _rank_metrics
    gold = [_rank_source("first")["source_spans"][0],
            _rank_source("second", 20, 30)["source_spans"][0]]
    metrics = _rank_metrics([_rank_source("first"), _rank_source("noise", 100, 110)], gold)
    expected = (1 / __import__("math").log2(2)) / (
        1 / __import__("math").log2(2) + 1 / __import__("math").log2(3))
    assert metrics["ndcg_at"]["10"] == pytest.approx(expected)
    assert metrics["ndcg_at"]["10"] < 1.0


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


def test_runtime_configuration_records_only_nonsecret_database_identity(monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.rag_eval.cli import runtime_configuration

    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://eval_user:super-secret@db.example:3307/knowledge?ssl=true")
    configuration = runtime_configuration(LearningSettings())
    assert configuration["database_identity"] == {
        "scheme": "mysql+pymysql", "host": "db.example", "port": 3307, "database": "knowledge"
    }
    assert "super-secret" not in json.dumps(configuration)
    assert "eval_user" not in json.dumps(configuration)
    assert "ssl=true" not in json.dumps(configuration)


def test_freeze_marks_missing_database_identity_as_legacy(evaluation):
    from knowpath_backend.rag_eval.dataset import freeze

    result = freeze(evaluation[0], evaluation[0].parent / "legacy.freeze.json")
    assert result["freeze_compatibility"] == "legacy_without_database_identity"


def test_database_identity_mismatch_is_rejected_before_database_open(monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.persistence import db
    from knowpath_backend.rag_eval import cli

    settings = LearningSettings()
    monkeypatch.setattr(LearningSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///eval-a.db")
    configuration = cli.runtime_configuration(settings)
    configuration.update(profile={"manifest_configuration_hashes": {}}, runtime_bindings={})
    monkeypatch.setenv("DATABASE_URL", "sqlite:///eval-b.db")
    monkeypatch.setattr(db, "create_db_engine", lambda: pytest.fail("database opened before identity check"))
    with pytest.raises(ValueError, match="frozen models/prompts/budgets/environment"):
        cli.configured_runtime({"config": configuration})


def test_legacy_freeze_is_rejected_before_database_open(monkeypatch):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.persistence import db
    from knowpath_backend.rag_eval import cli

    settings = LearningSettings()
    monkeypatch.setattr(LearningSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///eval-legacy.db")
    configuration = cli.runtime_configuration(settings)
    configuration.pop("database_identity")
    configuration.update(profile={"manifest_configuration_hashes": {}}, runtime_bindings={})
    monkeypatch.setattr(db, "create_db_engine", lambda: pytest.fail("legacy freeze opened database"))
    with pytest.raises(ValueError, match="legacy freeze missing database identity"):
        cli.configured_runtime({"config": configuration})


def test_report_exposes_usage_and_call_observability(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report

    def response():
        value = answer()
        value["trace"].update(generation_calls=1, verification_calls=1,
            usage=[], rerank_calls=1, rerank_usage=None,
            retrieval={"embedding_calls": 1}, embedding_usage=None)
        return value

    path = frozen(evaluation)
    output = evaluation[0].parent / "observability-runs.jsonl"
    run(path, output, pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: response()),
        scope_resolver=resolver)
    result = report(path, output)
    metrics = result["plugins"]["a"]
    assert metrics["usage_known_rate"] == 0
    assert metrics["cost_unknown_reason"] == {"missing_provider_usage": 14}
    assert metrics["call_counts"] == {
        "generation": 14, "verification": 14, "embedding": 14, "rerank": 14
    }


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


def test_frozen_modes_default_and_allowlist(evaluation):
    from knowpath_backend.rag_eval.dataset import EVALUATION_MODES, freeze
    config, configuration = evaluation
    destination = config.parent / "freeze-default.json"
    frozen = freeze(config, destination)
    assert frozen["config"]["modes"] == ["a", "b1"]
    for value in (["a", "a"], ["b1", "b15"], ["a", "unknown"]):
        configuration["modes"] = value
        config.write_text(json.dumps(configuration), encoding="utf-8")
        with pytest.raises(ValueError, match="modes"):
            freeze(config, config.parent / ("freeze-" + str(len(value)) + ".json"))
    assert set(EVALUATION_MODES) == {"a", "a_large", "b1", "b15", "b2_r1", "b3_unit", "parent_merge", "typed_edge", "a0", "f", "b4"}


def test_b2_r1_is_a_runtime_mode_with_fixed_budget_metadata():
    from knowpath_backend.rag_eval.dataset import mode_spec
    spec = mode_spec("b2_r1")
    assert spec["execution"] == "runtime"
    assert spec["budget"]["closure"] == "continuation_only"


def test_b3_unit_is_a_runtime_mode_with_fixed_budget_metadata():
    from knowpath_backend.rag_eval.dataset import mode_spec
    spec = mode_spec("b3_unit")
    assert spec["execution"] == "runtime"
    assert spec["budget"] == {"unit_admission": "rank_and_provenance", "candidate_limit": 40}


def test_b3_attribution_reports_unit_expansion_and_leaf_evidence():
    from knowpath_backend.rag_eval.scoring import _b3_attribution
    first = _rank_source("first")
    added = _rank_source("added", 20, 30)
    response = {"sources": [first, added], "trace": {"plugin": "b3_unit", "retrieval": {
        "unit_hits": ["first"],
        "unit_expansions": [{"anchor_id": "first", "leaf_ids": ["first", "added"]}],
        "rerank_groups": [{"group_id": "first", "leaf_ids": ["first", "added"],
                            "retrieval_text": "unit text"}],
        "structural_additions": 1,
        "a_retention_at_40": 1.0,
        "skipped_units": [],
        "fallback_reason": None,
    }, "candidate_sources": [first, added], "reranked_sources": [first, added]}}
    result = _b3_attribution(response, [added["source_spans"][0]], relation_type="continuation")
    assert result["unit_hit_count"] == 1
    assert result["unit_expansion_count"] == 1
    assert result["atomic_leaf_additions"] == 1
    assert result["added_evidence_recall"] is None
    assert result["added_evidence_recall_status"] == "missing_baseline"
    assert result["context_evidence_recall"] == 1.0
    assert result["structural_additions"] == 1
    assert result["a_retention_at_40"] == 1.0
    assert result["negative_control"] is False


def test_b3_attribution_marks_negative_control_expansion():
    from knowpath_backend.rag_eval.scoring import _b3_attribution
    first = _rank_source("first")
    added = _rank_source("added", 20, 30)
    response = {"trace": {"plugin": "b3_unit", "retrieval": {
        "unit_hits": ["first"],
        "unit_expansions": [{"anchor_id": "first", "leaf_ids": ["first", "added"]}],
        "structural_additions": 1, "a_retention_at_40": 1.0,
        "rerank_groups": [], "skipped_units": [], "fallback_reason": None,
    }, "candidate_sources": [first, added], "reranked_sources": [first, added]}}
    result = _b3_attribution(response, [added["source_spans"][0]], relation_type="single_leaf")
    assert result["negative_control"] is True
    assert result["negative_control_expansion"] is True


def test_b2_trace_attribution_reports_closure_and_negative_control_metrics():
    from knowpath_backend.rag_eval.scoring import _b2_attribution
    first = _rank_source("first")
    continuation = _rank_source("continuation", 20, 30)
    response = {"trace": {"retrieval": {
        "closures": [{"unit_id": "unit-1", "chunk_id": "continuation", "edge_type": "continuation"}],
        "structural_additions": 1, "a_retention_at_40": 0.9,
        "replacements": [{"chunk_id": "replaced"}],
        "skipped_closures": [{"unit_id": "unit-2", "reason": "context_budget"}],
        "extensions": [{"unit_id": "unit-1", "chunk_id": "continuation", "edge_type": "continuation"}],
    }, "reranked_sources": [first, continuation]}}
    result = _b2_attribution(response, [continuation["source_spans"][0]],
                              relation_type="single_leaf")
    assert result["continuation_unit_recall"] == 1.0
    assert result["continuation_unit_complete_recall"] == 1.0
    assert result["structural_additions"] == 1
    assert result["a_retention_at_40"] == 0.9
    assert result["replacement_count"] == 1
    assert result["skipped_closure_count"] == 1
    assert result["negative_control_expansion"] is True


def test_runner_rotates_four_modes_and_report_compares_to_a(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    config, configuration = evaluation
    configuration["modes"] = ["a", "a_large", "b1", "b15"]
    config.write_text(json.dumps(configuration), encoding="utf-8")
    freeze_path = frozen(evaluation)
    output = config.parent / "four-mode-runs.jsonl"
    calls = []
    class Pipeline:
        def __init__(self, mode): self.mode = mode
        def answer(self, *args, **kwargs):
            calls.append(self.mode)
            return answer()
    rows = run(freeze_path, output, pipeline_factory=Pipeline, scope_resolver=resolver)
    assert len(rows) == 7 * 2 * 4
    assert calls[:8] == ["a", "a_large", "b1", "b15", "a_large", "b1", "b15", "a"]
    reviews = [dict(question_id=r["question_id"], plugin=r["plugin"], repeat=r["repeat"],
                    success=r["plugin"] == "a", partial=False, error_type="context") for r in rows]
    review_path = config.parent / "four-mode-reviews.json"
    review_path.write_text(json.dumps(reviews), encoding="utf-8")
    result = report(freeze_path, output, review_path)
    assert set(result["plugins"]) == {"a", "a_large", "b1", "b15"}
    assert result["plugins"]["b15"]["b15_attribution"] is not None
    assert result["comparisons"]["a_large"]["success_rate_minus_a"] == -1
    assert result["comparisons"]["b15"]["success_rate_minus_a"] == -1


def test_b15_attribution_uses_only_explicit_extension_trace():
    from knowpath_backend.rag_eval.scoring import _tree_attribution
    span = {"material_version_id": "mv", "artifact_hash": "hash", "page": 1,
            "block": "block", "start": 0, "end": 10}
    response = {"trace": {"extensions": [{"chunk_id": "ext", "source_spans": [span]}],
                           "candidate_sources": [{"chunk_id": "noise", "source_spans": [span]}]}}
    result = _tree_attribution(response, [span])
    assert result["extension_count"] == 1
    assert result["extension_only_recall"] == 1.0


def test_ablation_modes_are_injected_only_with_frozen_budgets():
    from knowpath_backend.rag_eval.dataset import ablation_fallback, mode_spec
    parent = mode_spec("parent_merge")
    typed = mode_spec("typed_edge")
    assert parent["execution"] == "injected_offline"
    assert parent["budget"]["sibling_candidate_limit"] == 0
    assert typed["execution"] == "injected_offline"
    assert typed["budget"]["typed_edge_hops"] == 1
    assert not ablation_fallback("parent_merge", "parent_context")
    assert ablation_fallback("parent_merge", "same_section")
    assert not ablation_fallback("typed_edge", "typed_cross_reference")
    assert ablation_fallback("typed_edge", "single_leaf")


def test_offline_ablation_trace_requires_parent_or_provenance_relation():
    from knowpath_backend.rag_eval.scoring import _ablation_attribution
    parent = _ablation_attribution(
        {"trace": {"ablation": {"mode": "parent_merge", "trace_status": "complete",
            "parent_context_ids": ["parent"], "sibling_candidate_ids": []}}},
        "parent_merge", {"relation_type": "parent_context"})
    assert parent["eligible"] is True and parent["fallback"] is False
    assert parent["parent_context_count"] == 1 and parent["sibling_candidate_count"] == 0
    typed = _ablation_attribution(
        {"trace": {"ablation": {"mode": "typed_edge", "trace_status": "complete", "edges": [
            {"edge_type": "typed_cross_reference", "provenance": {"source": "review"}},
            {"edge_type": "typed_cross_reference"},
            {"edge_type": "same_section", "provenance": {"source": "review"}},
        ]}}}, "typed_edge", {"relation_type": "typed_cross_reference"})
    assert typed["typed_edge_count"] == 2
    assert typed["provenance_edge_count"] == 1
    fallback = _ablation_attribution({"trace": {}}, "typed_edge", {"relation_type": "single_leaf"})
    assert fallback["eligible"] is False and fallback["fallback"] is True


def test_real_runtime_refuses_offline_ablation_modes_without_model_calls(evaluation):
    from knowpath_backend.rag_eval.runner import run
    config, configuration = evaluation
    configuration["modes"] = ["a", "parent_merge"]
    config.write_text(json.dumps(configuration), encoding="utf-8")
    output = config.parent / "offline-ablation-runtime.jsonl"
    class RealFactory:
        real_runtime = True
        def __call__(self, mode):
            raise AssertionError("offline ablation must not call the live factory")
    rows = run(frozen(evaluation), output, pipeline_factory=RealFactory(), scope_resolver=resolver)
    assert len(rows) == 7 * 2 * 2
    assert all(row["error_code"] == "EVALUATION_MODE_OFFLINE_ONLY"
               for row in rows if row["plugin"] == "parent_merge")


def test_cli_rejects_offline_ablation_before_runtime_setup():
    from knowpath_backend.rag_eval.cli import configured_runtime
    with pytest.raises(ValueError, match="injected offline"):
        configured_runtime({"config": {"modes": ["a", "typed_edge"]}})


def test_report_accepts_valid_mode_set_without_b1(evaluation):
    from knowpath_backend.rag_eval.runner import run
    from knowpath_backend.rag_eval.scoring import report
    config, configuration = evaluation
    configuration["modes"] = ["a", "a_large"]
    config.write_text(json.dumps(configuration), encoding="utf-8")
    freeze_path = frozen(evaluation)
    output = config.parent / "a-large-runs.jsonl"
    run(freeze_path, output, pipeline_factory=lambda mode: SimpleNamespace(answer=lambda *a, **k: answer()),
        scope_resolver=resolver)
    result = report(freeze_path, output)
    assert set(result["plugins"]) == {"a", "a_large"}
    assert result["comparisons"]["a_large"]["success_rate_minus_a"] is None
