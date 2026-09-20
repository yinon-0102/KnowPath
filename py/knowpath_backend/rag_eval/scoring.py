"""Offline human scoring; model self-checks never establish answer quality."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from statistics import mean

from .dataset import GATES, numeric, read_json, verify_freeze


def _key(row):
    return row["question_id"], row["plugin"], row["repeat"]


def _quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def evidence_coverage(response, gold, field):
    """Offline coordinate coverage, not a semantic support or correctness claim."""
    required = gold.get("necessary_evidence", [])
    if not required or response is None:
        return None
    container = response.get('trace', {}) if field in {'candidate_sources', 'reranked_sources'} else response
    if field not in container:
        return None
    spans = [span for item in container.get(field, []) for span in item.get("source_spans", [])]
    def covered(target):
        identity = ("material_version_id", "artifact_hash", "page", "block")
        intervals = sorted((span["start"], span["end"]) for span in spans
            if all(span.get(key) == target.get(key) for key in identity))
        cursor = target["start"]
        for start, end in intervals:
            if start > cursor:
                break
            cursor = max(cursor, end)
            if cursor >= target["end"]:
                return True
        return False
    return sum(covered(span) for span in required) / len(required)


def _aggregate(items):
    denominator = len(items)
    serviced = [r for r in items if r["service_success"]]
    known = [r for r in items if r["quality"] is not None]
    full = sum(r["quality"] is True for r in known)
    partial = sum(r["partial"] is True for r in items)
    latency = [r["latency_ms"] for r in items if numeric(r.get("latency_ms"))]
    costs = [r["cost"] for r in items if numeric(r.get("cost"))]
    context_coverage = [r["context_evidence_coverage"] for r in items if r.get("context_evidence_coverage") is not None]
    citation_coverage = [r["citation_evidence_coverage"] for r in items if r.get("citation_evidence_coverage") is not None]
    candidate_coverage = [r['candidate_evidence_coverage'] for r in items if r.get('candidate_evidence_coverage') is not None]
    rerank_coverage = [r['rerank_evidence_coverage'] for r in items if r.get('rerank_evidence_coverage') is not None]
    unknown = len(items) - len(known)
    return dict(denominator=denominator, service_success_denominator=len(serviced),
        service_failures=sum(not r["service_success"] and not r.get("missing") for r in items),
        missing_execution_rows=sum(bool(r.get("missing")) for r in items),
        human_review_missing=sum(r["service_success"] and r["quality"] is None for r in items),
        full_successes=full, partial_successes=partial, unknown_quality=unknown,
        success_rate=full / denominator if denominator and not unknown else None,
        known_success_lower_bound=full / denominator if denominator else None,
        partial_rate=partial / denominator if denominator and not unknown else None,
        service_subset_success_rate=full / len(serviced) if serviced and not any(r["quality"] is None for r in serviced) else None,
        service_failure_rate=sum(not r["service_success"] and not r.get("missing") for r in items) / denominator if denominator else None,
        p50_latency_ms=_quantile(latency, .5), p95_latency_ms=_quantile(latency, .95), latency_measured=len(latency),
        mean_cost_per_request=mean(costs) if len(costs) == denominator and denominator else None,
        cost_measured=len(costs), known_cost_total=sum(costs) if costs else None,
        context_evidence_coverage=mean(context_coverage) if context_coverage else None,
        context_coverage_denominator=len(context_coverage),
        citation_evidence_coverage=mean(citation_coverage) if citation_coverage else None,
        citation_coverage_denominator=len(citation_coverage),
        candidate_evidence_coverage=mean(candidate_coverage) if candidate_coverage else None,
        candidate_coverage_denominator=len(candidate_coverage),
        rerank_evidence_coverage=mean(rerank_coverage) if rerank_coverage else None,
        rerank_coverage_denominator=len(rerank_coverage),
        error_types=dict(Counter(r["error_type"] for r in items if r.get("error_type"))))


def report(freeze_path, results_path, reviews_path=None):
    frozen, dataset, split = verify_freeze(freeze_path)
    records = [json.loads(line) for line in Path(results_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    partitions = {r["partition"] for r in records}
    if len(partitions) != 1 or not partitions <= {"dev", "heldout"}:
        raise ValueError("results must contain exactly one partition")
    partition = next(iter(partitions))
    selected = {r["question_id"]: r for r in dataset if r["question_id"] in split[partition]["question_ids"]}
    expected = {(qid, mode, repeat) for qid in selected for mode in ("a", "b1") for repeat in range(frozen["config"]["repeats"])}
    by_key = {}
    for row in records:
        key = _key(row)
        if key not in expected or key in by_key or row["freeze_id"] != frozen["freeze_id"]:
            raise ValueError("duplicate, unfrozen or unscheduled result")
        if type(row.get("service_success")) is not bool:
            raise ValueError("invalid service result")
        by_key[key] = row
    reviews = read_json(reviews_path) if reviews_path else []
    judgments = {}
    for review in reviews:
        key = _key(review)
        if (key not in expected or key in judgments or type(review.get("success")) is not bool
                or type(review.get("partial")) is not bool or review["success"] and review["partial"]
                or not isinstance(review.get("error_type"), str)):
            raise ValueError("invalid/duplicate human review")
        judgments[key] = review
    enriched = []
    for key in sorted(expected):
        qid, mode, repeat = key
        record = dict(by_key.get(key, dict(question_id=qid, plugin=mode, repeat=repeat,
            service_success=False, missing=True, cost=None, latency_ms=None)))
        record["family_id"] = selected[qid]["family_id"]
        record["context_evidence_coverage"] = evidence_coverage(record.get("response"), selected[qid], "sources")
        record["citation_evidence_coverage"] = evidence_coverage(record.get("response"), selected[qid], "citations")
        record['candidate_evidence_coverage'] = evidence_coverage(record.get('response'), selected[qid], 'candidate_sources')
        record['rerank_evidence_coverage'] = evidence_coverage(record.get('response'), selected[qid], 'reranked_sources')
        record.update(quality=None, partial=None, error_type=None)
        if not record.get("missing") and not record["service_success"]:
            record.update(quality=False, partial=False, error_type="service")
        elif record["service_success"] and key in judgments:
            review = judgments[key]
            # An honest partial answer is never a full-task success, even if a
            # reviewer accidentally marks its supported subset as successful.
            partial = review["partial"] or record.get("answer_status") == "partial"
            record.update(quality=review["success"] and not partial, partial=partial, error_type=review["error_type"])
        enriched.append(record)
    plugins = {mode: _aggregate([r for r in enriched if r["plugin"] == mode]) for mode in ("a", "b1")}
    questions, families = {}, defaultdict(list)
    paired = dict(a_wins=0, b1_wins=0, ties=0, unknown=0, question_denominator=len(selected))
    for qid, source in selected.items():
        group = [r for r in enriched if r["question_id"] == qid]
        scores = {mode: _aggregate([r for r in group if r["plugin"] == mode])["success_rate"] for mode in ("a", "b1")}
        delta = None if None in scores.values() else scores["b1"] - scores["a"]
        questions[qid] = dict(family_id=source["family_id"], **scores, b1_minus_a=delta)
        families[source["family_id"]].append(delta)
        paired["unknown" if delta is None else "b1_wins" if delta > 0 else "a_wins" if delta < 0 else "ties"] += 1
    family_results = {name: dict(questions=len(values), b1_minus_a=None if None in values else mean(values)) for name, values in families.items()}
    differences = [r["b1_minus_a"] for r in family_results.values() if r["b1_minus_a"] is not None]
    paired.update(independent_families=len(families),
        family_equal_weighted_difference=mean(differences) if len(differences) == len(families) and differences else None,
        descriptive_family_range=[min(differences), max(differences)] if differences else None,
        uncertainty="descriptive family range only; no confidence interval or adoption claim")
    def ratio(metric):
        a, b = plugins["a"][metric], plugins["b1"][metric]
        if metric == "p95_latency_ms" and any(p["latency_measured"] != p["denominator"] for p in plugins.values()):
            return None
        return b / a if a is not None and a > 0 and b is not None else None
    measures = {"max_p95_latency_ms": max(p["p95_latency_ms"] for p in plugins.values())
                    if all(p["latency_measured"] == p["denominator"] for p in plugins.values()) else None,
                "max_mean_cost_per_request": max(p["mean_cost_per_request"] for p in plugins.values())
                    if all(p["mean_cost_per_request"] is not None for p in plugins.values()) else None,
                "max_b1_latency_ratio": ratio("p95_latency_ms"), "max_b1_cost_ratio": ratio("mean_cost_per_request")}
    checks = {name: (measures[name] <= frozen["config"]["gates"][name]
                    if measures[name] is not None and numeric(frozen["config"]["gates"].get(name), positive=True) else None) for name in GATES}
    categories = sorted({c for source in selected.values() for c in source.get("categories", [])})
    breakdown = {category: {mode: _aggregate([r for r in enriched if r["plugin"] == mode and
        category in selected[r["question_id"]].get("categories", [])]) for mode in ("a", "b1")} for category in categories}
    return dict(freeze_id=frozen["freeze_id"], partition=partition, plugins=plugins, paired=paired,
        questions=questions, families=family_results, categories=breakdown,
        missing_execution_rows=len(expected) - len(records),
        gates=dict(measured=measures, checks=checks, passed=False if False in checks.values() else None if None in checks.values() else True),
        decision="exploratory_no_adoption", evidence_kind=sorted({r.get("evidence_kind", "unknown") for r in records}),
        quality_source="external human review only", build_update_cost="not measured by request harness")
