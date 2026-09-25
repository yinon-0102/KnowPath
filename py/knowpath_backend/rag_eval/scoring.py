"""Offline human scoring; model self-checks never establish answer quality."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from statistics import mean

from .dataset import GATES, ablation_fallback, evaluation_modes, mode_spec, numeric, read_json, verify_freeze


RANK_KS = (1, 3, 5, 10, 20, 40)
SOURCE_IDENTITY = ("material_version_id", "artifact_hash", "page", "block")
METRIC_DEFINITIONS = {
    "candidate_ranking": "Legacy single-leaf full-span Recall/MRR/NDCG; unchanged.",
    "reranked_ranking": "Legacy single-leaf full-span Recall/MRR/NDCG; unchanged.",
    "candidate_union_ranking": "Recall@K: fraction of gold spans fully covered by the coordinate union of the first K candidate leaves, matching material version, artifact, page and block; gaps never count. Macro mean over measured questions/repeats.",
    "reranked_union_ranking": "Same coordinate-union Recall@K over the first K reranked leaves; no union NDCG is defined.",
    "b3_added_evidence_recall": "Newly fully covered gold spans after adding actual B3 candidate leaves absent from A to the full A candidate baseline, divided by all gold spans. This is incremental evidence potential, not final-pool retention or answer quality. Missing/incomplete baseline is unknown; aggregate mean includes only known rows with explicit denominators.",
    "b3_unit_expansion_count": "Distinct anchors whose expansion contains more than one distinct leaf; unit_expansion_record_count retains all valid diagnostic records including singletons.",
}


def _key(row):
    return row["question_id"], row["plugin"], row["repeat"]


def _quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _span_covers(span, target):
    return (all(span.get(key) == target.get(key) for key in SOURCE_IDENTITY)
            and span.get("start", 0) <= target.get("start", 0)
            and span.get("end", 0) >= target.get("end", 0))


def _rank_metrics(items, required):
    """Calculate exact-coordinate ranking metrics for one ordered source list."""
    if not required or items is None:
        return None
    rows = list(items)
    relevant = [any(_span_covers(source_span, target)
                    for target in required
                    for source_span in item.get("source_spans", []))
                for item in rows]
    first_hit = next((index + 1 for index, value in enumerate(relevant) if value), None)
    recall = {}
    ndcg = {}
    for k in RANK_KS:
        covered = sum(any(_span_covers(source_span, target)
                         for source_span in item.get("source_spans", []))
                      for target in required for item in rows[:k])
        # The inner sum above counts a target once per item.  Collapse it to a
        # set of target indexes so duplicate chunks cannot inflate Recall@K.
        covered_ids = {index for index, target in enumerate(required)
                       if any(_span_covers(source_span, target)
                              for item in rows[:k] for source_span in item.get("source_spans", []))}
        del covered
        recall[str(k)] = len(covered_ids) / len(required)
        dcg = sum((1 / math.log2(index + 2)) for index, value in enumerate(relevant[:k]) if value)
        # The denominator is fixed by the frozen gold set.  Deriving it from
        # the returned list makes NDCG self-normalizing: a run that retrieves
        # only one of two gold spans would incorrectly score 1.0.
        ideal_count = min(k, len(required))
        ideal = sum(1 / math.log2(index + 2) for index in range(ideal_count))
        ndcg[str(k)] = dcg / ideal if ideal else 0.0
    return {"gold_spans": len(required), "list_length": len(rows),
            "recall_at": recall, "mrr": 1 / first_hit if first_hit else 0.0,
            "first_hit_rank": first_hit, "ndcg_at": ndcg}


def _mean_rank_metrics(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {"questions": len(values), "gold_spans": sum(value["gold_spans"] for value in values),
            "recall_at": {key: mean(value["recall_at"][key] for value in values) for key in map(str, RANK_KS)},
            "mrr": mean(value["mrr"] for value in values),
            "ndcg_at": {key: mean(value["ndcg_at"][key] for value in values) for key in map(str, RANK_KS)},
            "first_hit_questions": sum(value["first_hit_rank"] is not None for value in values),
            "no_hit_questions": sum(value["first_hit_rank"] is None for value in values)}


def _union_covered_ids(items, required):
    """Return fully covered gold indexes, merging only identical source coordinates."""
    spans = [span for item in items for span in item.get("source_spans", [])]
    covered = set()
    for index, target in enumerate(required):
        intervals = sorted((span["start"], span["end"]) for span in spans
            if all(span.get(key) == target.get(key) for key in SOURCE_IDENTITY))
        cursor = target["start"]
        for start, end in intervals:
            if start > cursor:
                break
            cursor = max(cursor, end)
            if cursor >= target["end"]:
                covered.add(index)
                break
    return covered


def _union_rank_metrics(items, required):
    """Recall from full coordinate-union coverage within each ranked leaf prefix."""
    if not required or items is None:
        return None
    rows = list(items)
    return {"gold_spans": len(required), "list_length": len(rows),
            "recall_at": {str(k): len(_union_covered_ids(rows[:k], required)) / len(required)
                          for k in RANK_KS}}


def _mean_union_rank_metrics(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {"questions": len(values), "gold_spans": sum(value["gold_spans"] for value in values),
            "recall_at": {str(k): mean(value["recall_at"][str(k)] for value in values)
                          for k in RANK_KS}}


def _b3_incremental_recall(trace, retrieval, required):
    """Compare full A coverage with A plus leaves actually admitted by B3."""
    if not required:
        return None, "missing_gold"
    candidates = trace.get("candidate_sources", retrieval.get("candidate_sources"))
    if not isinstance(candidates, list):
        return None, "missing_candidates"
    source_map = {}
    for container in (retrieval, trace):
        for field in ("candidate_sources", "reranked_sources"):
            for item in container.get(field, []) or []:
                if isinstance(item, dict) and item.get("chunk_id") and isinstance(item.get("source_spans"), list):
                    source_map[item["chunk_id"]] = item
    baseline_ids = retrieval.get("baseline_candidate_ids")
    baseline_sources = retrieval.get("baseline_candidate_sources")
    if baseline_sources is not None:
        if not isinstance(baseline_sources, list) or any(
                not isinstance(item, dict) or not item.get("chunk_id")
                or not isinstance(item.get("source_spans"), list) for item in baseline_sources):
            return None, "incomplete_baseline"
        baseline_map = {item["chunk_id"]: item for item in baseline_sources}
        if baseline_ids is not None and (not isinstance(baseline_ids, list) or any(
                not isinstance(identifier, str) or identifier not in baseline_map for identifier in baseline_ids)):
            return None, "incomplete_baseline"
    elif baseline_ids is None:
        return None, "missing_baseline"
    elif not isinstance(baseline_ids, list) or any(
            not isinstance(identifier, str) or identifier not in source_map for identifier in baseline_ids):
        return None, "incomplete_baseline"
    else:
        baseline_map = {identifier: source_map[identifier] for identifier in baseline_ids}
    if any(not isinstance(item, dict) or not item.get("chunk_id")
           or not isinstance(item.get("source_spans"), list) for item in candidates):
        return None, "incomplete_candidates"
    baseline = list(baseline_map.values())
    added = [item for item in candidates if item["chunk_id"] not in baseline_map]
    before = _union_covered_ids(baseline, required)
    after = _union_covered_ids(baseline + added, required)
    return len(after - before) / len(required), "known"


def _tree_attribution(response, required):
    """Attribute only explicitly traced extension chunks to coordinate gold spans.

    This is an offline retrieval attribution metric.  It does not score answer
    correctness or turn gold evidence into a business or support score.
    """
    if not required or not response:
        return None
    trace = response.get("trace", {})
    retrieval = trace.get("retrieval", trace)
    extensions = retrieval.get("extensions", []) or []
    candidates = {item.get("chunk_id"): item for item in trace.get("candidate_sources", [])}
    # Some runtimes emit complete extension source records while others emit
    # only ids.  Prefer the explicit records and resolve ids through the frozen
    # candidate trace when needed; never infer extensions from candidate order.
    extension_items = []
    explicit_ids = set()
    seen = set()
    for entry in extensions:
        if isinstance(entry, dict):
            identifier = entry.get("chunk_id")
            item = entry if entry.get("source_spans") is not None else candidates.get(identifier)
        elif isinstance(entry, str):
            identifier = entry
            item = candidates.get(identifier)
        else:
            continue
        if identifier is None:
            continue
        explicit_ids.add(identifier)
        if item is not None and identifier not in seen:
            extension_items.append(item)
            seen.add(identifier)
    hit_ids = {identifier for identifier, item in ((item.get("chunk_id"), item) for item in extension_items)
               if any(_span_covers(source_span, target)
                      for source_span in item.get("source_spans", []) for target in required)}
    return {"extension_count": len(explicit_ids), "extension_hit_count": len(hit_ids),
            "extension_precision": len(hit_ids) / len(explicit_ids) if explicit_ids else None,
            "extension_only_recall": len({index for index, target in enumerate(required)
                                           if any(_span_covers(source_span, target)
                                           for item in extension_items
                                           for source_span in item.get("source_spans", []))}) / len(required)}


def _b2_attribution(response, required, *, relation_type=None):
    """Summarize B2-R1 continuation closure trace using frozen evidence only."""
    if not response:
        return None
    trace = response.get("trace", {}) or {}
    retrieval = trace.get("retrieval", trace)
    if not isinstance(retrieval, dict):
        return None
    if trace.get("plugin") != "b2_r1" and not any(
            key in retrieval for key in ("closures", "structural_additions", "a_retention_at_40")):
        return None
    closures = retrieval.get("closures", []) or []
    if not isinstance(closures, list):
        closures = []
    candidates = {}
    for field in ("candidate_sources", "reranked_sources"):
        for item in (trace.get(field, []) or retrieval.get(field, []) or []):
            if isinstance(item, dict) and item.get("chunk_id"):
                candidates[item["chunk_id"]] = item
    closure_items = []
    unit_seed_ids = defaultdict(set)
    closure_ids = set()
    units = defaultdict(set)
    for entry in closures:
        if not isinstance(entry, dict) or entry.get("edge_type") != "continuation":
            continue
        identifier = entry.get("chunk_id")
        if not identifier or identifier in closure_ids:
            continue
        item = entry if entry.get("source_spans") is not None else candidates.get(identifier)
        closure_ids.add(identifier)
        if item is not None:
            closure_items.append(item)
        if entry.get("unit_id"):
            units[entry["unit_id"]].add(identifier)
            if entry.get("seed_id"):
                unit_seed_ids[entry["unit_id"]].add(entry["seed_id"])
    # A closure trace lists additions, while the seed is already present in
    # the ordinary candidate list. Include that seed when measuring whether
    # the whole continuation unit's gold evidence is present.
    unit_complete_items = list(closure_items)
    seen_complete = {item.get("chunk_id") for item in unit_complete_items}
    for seed_ids in unit_seed_ids.values():
        for identifier in seed_ids:
            item = candidates.get(identifier)
            if item is not None and identifier not in seen_complete:
                unit_complete_items.append(item)
                seen_complete.add(identifier)
    added_hit_targets = {index for index, target in enumerate(required or []) if any(
        _span_covers(source_span, target)
        for item in closure_items for source_span in item.get("source_spans", []))}
    hit_targets = {index for index, target in enumerate(required or []) if any(
        _span_covers(source_span, target)
        for item in unit_complete_items for source_span in item.get("source_spans", []))}
    skipped = retrieval.get("skipped_closures", []) or []
    if not isinstance(skipped, list):
        skipped = []
    additions = retrieval.get("structural_additions", 0)
    if type(additions) is not int or additions < 0:
        additions = 0
    replacements = retrieval.get("replacements", []) or []
    if not isinstance(replacements, list):
        replacements = []
    retention = retrieval.get("a_retention_at_40")
    negative = relation_type in {"single_leaf", "adjacency_negative", "adjacent_noise"}
    return {
        "closure_count": len(closure_ids),
        "closure_unit_count": len(units),
        "continuation_unit_recall": len(hit_targets) / len(required) if required else None,
        "continuation_unit_complete_recall": 1.0 if required and len(hit_targets) == len(required) else 0.0 if required else None,
        "added_evidence_recall": len(added_hit_targets) / len(required) if required else None,
        "structural_additions": additions,
        "a_retention_at_40": retention if numeric(retention) else None,
        "replacement_count": len(replacements),
        "skipped_closure_count": len(skipped),
        "negative_control": negative,
        "negative_control_expansion": bool(negative and additions > 0),
    }


def _b3_attribution(response, required, *, relation_type=None):
    """Summarize B3 semantic-unit admission using only explicit runtime trace.

    Unit metadata is an execution/provenance diagnostic.  Evidence recall is
    still computed against frozen coordinate spans and never from unit labels.
    """
    if not response:
        return None
    trace = response.get("trace", {}) or {}
    retrieval = trace.get("retrieval", trace)
    if not isinstance(retrieval, dict):
        return None
    if trace.get("plugin") != "b3_unit" and not any(
            key in retrieval for key in ("unit_hits", "unit_expansions", "rerank_groups")):
        return None

    def _list(name):
        value = retrieval.get(name, [])
        return value if isinstance(value, list) else []

    hits = [value for value in _list("unit_hits") if isinstance(value, str) and value]
    expansions = [value for value in _list("unit_expansions")
                  if isinstance(value, dict) and isinstance(value.get("leaf_ids"), list)]
    expansion_ids = set()
    added_ids = set()
    for expansion in expansions:
        anchor = expansion.get("anchor_id")
        leaves = [value for value in expansion.get("leaf_ids", [])
                  if isinstance(value, str) and value]
        if anchor and len(set(leaves)) > 1:
            expansion_ids.add(anchor)
        added_ids.update(identifier for identifier in leaves if identifier != anchor)

    added_recall, added_status = _b3_incremental_recall(trace, retrieval, required)
    context_recall = evidence_coverage(response, {"necessary_evidence": required or []}, "sources")
    additions = retrieval.get("structural_additions", 0)
    if type(additions) is not int or additions < 0:
        additions = 0
    retention = retrieval.get("a_retention_at_40")
    skipped = _list("skipped_units")
    negative = relation_type in {"single_leaf", "adjacency_negative", "adjacent_noise"}
    fallback_reason = retrieval.get("fallback_reason")
    return {
        "unit_hit_count": len(set(hits)),
        "unit_expansion_count": len(expansion_ids),
        "unit_expansion_record_count": len(expansions),
        "atomic_leaf_additions": len(added_ids),
        "added_evidence_recall": added_recall,
        "added_evidence_recall_status": added_status,
        "context_evidence_recall": context_recall,
        "structural_additions": additions,
        "a_retention_at_40": retention if numeric(retention) else None,
        "skipped_unit_count": len(skipped),
        "negative_control": negative,
        "negative_control_expansion": bool(negative and additions > 0),
        "fallback_reason": fallback_reason if isinstance(fallback_reason, str) else None,
    }


def evidence_coverage(response, gold, field):
    """Offline coordinate coverage, not a semantic support or correctness claim."""
    required = gold.get("necessary_evidence", [])
    if not required or response is None:
        return None
    container = response.get('trace', {}) if field in {'candidate_sources', 'reranked_sources'} else response
    if field not in container:
        return None
    return len(_union_covered_ids(container.get(field, []), required)) / len(required)


def _ablation_attribution(response, mode, source):
    """Summarize an injected ablation trace without treating gold as a score.

    ``parent_merge`` may expose parent context ids but must not add sibling
    candidates. ``typed_edge`` may expose only typed edges carrying provenance.
    Missing relation metadata deterministically falls back to A in this
    offline report. These fields describe execution/trace quality only.
    """
    spec = mode_spec(mode)
    if spec["execution"] != "injected_offline":
        return None
    relation_type = source.get("relation_type")
    eligible = not ablation_fallback(mode, relation_type)
    trace = (response or {}).get("trace", {}) if response else {}
    value = trace.get("ablation") if isinstance(trace.get("ablation"), dict) else {}
    reported_mode = value.get("mode")
    if reported_mode not in (None, mode):
        return {"mode": mode, "relation_type": relation_type, "eligible": eligible,
                "fallback": True, "fallback_reason": "trace_mode_mismatch",
                "trace_status": "invalid", "parent_context_count": 0,
                "typed_edge_count": 0, "provenance_edge_count": 0,
                "sibling_candidate_count": 0}
    parent_ids = value.get("parent_context_ids", [])
    if not isinstance(parent_ids, list):
        parent_ids = []
    sibling_ids = value.get("sibling_candidate_ids", [])
    if not isinstance(sibling_ids, list):
        sibling_ids = []
    edges = value.get("typed_edges", value.get("edges", []))
    if not isinstance(edges, list):
        edges = []
    typed_edges = [edge for edge in edges if isinstance(edge, dict)
                   and isinstance(edge.get("edge_type"), str)
                   and edge.get("edge_type") in spec.get("relation_types", ())]
    provenance_edges = [edge for edge in typed_edges if isinstance(edge.get("provenance"), dict)
                        and edge["provenance"]]
    fallback = (not eligible or value.get("fallback") in {True, "relation_unavailable"}
                or value.get("fallback_reason") == "relation_unavailable")
    status = value.get("trace_status", "complete" if value else "missing")
    if status not in {"complete", "fallback", "missing", "invalid"}:
        status = "invalid"
    return {"mode": mode, "relation_type": relation_type, "eligible": eligible,
            "fallback": fallback,
            "fallback_reason": "relation_unavailable" if not eligible else value.get("fallback_reason"),
            "trace_status": status,
            "parent_context_count": len(parent_ids) if mode == "parent_merge" else 0,
            "typed_edge_count": len(typed_edges) if mode == "typed_edge" else 0,
            "provenance_edge_count": len(provenance_edges) if mode == "typed_edge" else 0,
            "sibling_candidate_count": len(sibling_ids) if mode == "parent_merge" else 0}


def _aggregate(items, pricing=None):
    denominator = len(items)
    serviced = [r for r in items if r["service_success"]]
    known = [r for r in items if r["quality"] is not None]
    full = sum(r["quality"] is True for r in known)
    partial = sum(r["partial"] is True for r in items)
    latency = [r["latency_ms"] for r in items if numeric(r.get("latency_ms"))]
    costs = [r["cost"] for r in items if numeric(r.get("cost"))]
    traces = [((r.get("response") or {}).get("trace") or {}) for r in items]
    call_counts = {
        "generation": sum(t.get("generation_calls", 0) for t in traces if type(t.get("generation_calls", 0)) is int),
        "verification": sum(t.get("verification_calls", 0) for t in traces if type(t.get("verification_calls", 0)) is int),
        "embedding": sum(((t.get("retrieval") or {}).get("embedding_calls", 0)) for t in traces
                          if type((t.get("retrieval") or {}).get("embedding_calls", 0)) is int),
        "rerank": sum(t.get("rerank_calls", 0) for t in traces if type(t.get("rerank_calls", 0)) is int),
    }
    usage_known = 0
    unknown_reasons = Counter()
    for row, trace in zip(items, traces):
        if not row.get("response"):
            unknown_reasons["no_response"] += 1
            continue
        expected_chat = trace.get("generation_calls", 0) + trace.get("verification_calls", 0)
        usage = trace.get("usage")
        complete_chat = expected_chat == 0 or (isinstance(usage, list) and len(usage) == expected_chat and all(
            isinstance(item, dict) and item.get("complete", True) is not False for item in usage)
        )
        embedding_calls = ((trace.get("retrieval") or {}).get("embedding_calls", 0))
        embedding = trace.get("embedding_usage")
        complete_embedding = not embedding_calls or isinstance(embedding, dict) and embedding.get("complete") is True
        rerank_calls = trace.get("rerank_calls", 0)
        complete_rerank = not rerank_calls or isinstance(trace.get("rerank_usage"), dict)
        if complete_chat and complete_embedding and complete_rerank:
            usage_known += 1
            if row.get("cost") is None:
                unknown_reasons["missing_frozen_pricing" if not (pricing or {}).get("price_table")
                                or not (pricing or {}).get("date") else "cost_not_derived"] += 1
        else:
            unknown_reasons["missing_provider_usage"] += 1
    usage_denominator = len(items)
    context_coverage = [r["context_evidence_coverage"] for r in items if r.get("context_evidence_coverage") is not None]
    citation_coverage = [r["citation_evidence_coverage"] for r in items if r.get("citation_evidence_coverage") is not None]
    candidate_coverage = [r['candidate_evidence_coverage'] for r in items if r.get('candidate_evidence_coverage') is not None]
    rerank_coverage = [r['rerank_evidence_coverage'] for r in items if r.get('rerank_evidence_coverage') is not None]
    candidate_ranking = _mean_rank_metrics([r.get("candidate_ranking") for r in items])
    reranked_ranking = _mean_rank_metrics([r.get("reranked_ranking") for r in items])
    candidate_union_ranking = _mean_union_rank_metrics([r.get("candidate_union_ranking") for r in items])
    reranked_union_ranking = _mean_union_rank_metrics([r.get("reranked_union_ranking") for r in items])
    tree_values = [r.get("tree_attribution") for r in items if r.get("tree_attribution") is not None]
    tree = None
    if tree_values:
        extension_count = sum(value["extension_count"] for value in tree_values)
        extension_hit_count = sum(value["extension_hit_count"] for value in tree_values)
        tree = {"questions": len(tree_values), "extension_count": extension_count,
                "extension_hit_count": extension_hit_count,
                "extension_precision": extension_hit_count / extension_count if extension_count else None,
                "extension_only_recall": mean(value["extension_only_recall"] for value in tree_values)}
    b2_values = [r.get("b2_attribution") for r in items if r.get("b2_attribution") is not None]
    b2 = None
    if b2_values:
        negative_values = [value for value in b2_values if value["negative_control"]]
        b2 = {
            "questions": len(b2_values),
            "closure_count": sum(value["closure_count"] for value in b2_values),
            "closure_unit_count": sum(value["closure_unit_count"] for value in b2_values),
            "continuation_unit_recall": mean(value["continuation_unit_recall"] for value in b2_values
                                              if value["continuation_unit_recall"] is not None),
            "continuation_unit_complete_recall": mean(value["continuation_unit_complete_recall"] for value in b2_values
                                                       if value["continuation_unit_complete_recall"] is not None),
            "added_evidence_recall": mean(value["added_evidence_recall"] for value in b2_values
                                           if value["added_evidence_recall"] is not None),
            "structural_additions": sum(value["structural_additions"] for value in b2_values),
            "a_retention_at_40": mean(value["a_retention_at_40"] for value in b2_values
                                       if value["a_retention_at_40"] is not None),
            "replacement_count": sum(value["replacement_count"] for value in b2_values),
            "skipped_closure_count": sum(value["skipped_closure_count"] for value in b2_values),
            "negative_control_questions": len(negative_values),
            "negative_control_expansion_rate": (
                sum(value["negative_control_expansion"] for value in negative_values) / len(negative_values)
                if negative_values else None),
        }
    b3_values = [r.get("b3_attribution") for r in items if r.get("b3_attribution") is not None]
    b3 = None
    if b3_values:
        negative_values = [value for value in b3_values if value["negative_control"]]
        def b3_mean(field):
            measured = [value[field] for value in b3_values if value[field] is not None]
            return mean(measured) if measured else None
        added_denominator = sum(value["added_evidence_recall"] is not None for value in b3_values)
        b3 = {
            "questions": len(b3_values),
            "unit_hit_count": sum(value["unit_hit_count"] for value in b3_values),
            "unit_expansion_count": sum(value["unit_expansion_count"] for value in b3_values),
            "unit_expansion_record_count": sum(value["unit_expansion_record_count"] for value in b3_values),
            "atomic_leaf_additions": sum(value["atomic_leaf_additions"] for value in b3_values),
            "added_evidence_recall": b3_mean("added_evidence_recall"),
            "added_evidence_recall_denominator": added_denominator,
            "added_evidence_recall_unknown": len(b3_values) - added_denominator,
            "added_evidence_recall_status": dict(Counter(value["added_evidence_recall_status"] for value in b3_values)),
            "context_evidence_recall": b3_mean("context_evidence_recall"),
            "structural_additions": sum(value["structural_additions"] for value in b3_values),
            "a_retention_at_40": b3_mean("a_retention_at_40"),
            "skipped_unit_count": sum(value["skipped_unit_count"] for value in b3_values),
            "fallback_count": sum(bool(value["fallback_reason"]) for value in b3_values),
            "negative_control_questions": len(negative_values),
            "negative_control_expansion_rate": (sum(value["negative_control_expansion"] for value in negative_values) / len(negative_values) if negative_values else None),
        }
    ablation_values = [r.get("ablation_attribution") for r in items
                       if r.get("ablation_attribution") is not None]
    ablation = None
    if ablation_values:
        typed_values = [value for value in ablation_values if value["typed_edge_count"]]
        ablation = {
            "questions": len(ablation_values),
            "eligible_questions": sum(value["eligible"] for value in ablation_values),
            "fallback_count": sum(value["fallback"] for value in ablation_values),
            "trace_complete": sum(value["trace_status"] == "complete" for value in ablation_values),
            "trace_missing": sum(value["trace_status"] == "missing" for value in ablation_values),
            "trace_invalid": sum(value["trace_status"] == "invalid" for value in ablation_values),
            "parent_context_count": sum(value["parent_context_count"] for value in ablation_values),
            "typed_edge_count": sum(value["typed_edge_count"] for value in ablation_values),
            "provenance_edge_count": sum(value["provenance_edge_count"] for value in ablation_values),
            "sibling_candidate_count": sum(value["sibling_candidate_count"] for value in ablation_values),
            "parent_merge_sibling_free": all(value["sibling_candidate_count"] == 0
                                              for value in ablation_values),
            "typed_edge_provenance_complete": bool(typed_values) and all(
                value["provenance_edge_count"] == value["typed_edge_count"]
                for value in typed_values),
        }
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
        usage_known_rate=usage_known / usage_denominator if usage_denominator else None,
        cost_unknown_reason=dict(unknown_reasons),
        call_counts=call_counts,
        context_evidence_coverage=mean(context_coverage) if context_coverage else None,
        context_coverage_denominator=len(context_coverage),
        citation_evidence_coverage=mean(citation_coverage) if citation_coverage else None,
        citation_coverage_denominator=len(citation_coverage),
        candidate_evidence_coverage=mean(candidate_coverage) if candidate_coverage else None,
        candidate_coverage_denominator=len(candidate_coverage),
        rerank_evidence_coverage=mean(rerank_coverage) if rerank_coverage else None,
        rerank_coverage_denominator=len(rerank_coverage),
        candidate_ranking=candidate_ranking, reranked_ranking=reranked_ranking,
        candidate_union_ranking=candidate_union_ranking, reranked_union_ranking=reranked_union_ranking,
        tree_attribution=tree, b2_attribution=b2, b3_attribution=b3, ablation_attribution=ablation,
        error_types=dict(Counter(r["error_type"] for r in items if r.get("error_type"))))


def report(freeze_path, results_path, reviews_path=None):
    frozen, dataset, split = verify_freeze(freeze_path)
    records = [json.loads(line) for line in Path(results_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    partitions = {r["partition"] for r in records}
    if len(partitions) != 1 or not partitions <= {"dev", "heldout"}:
        raise ValueError("results must contain exactly one partition")
    partition = next(iter(partitions))
    selected = {r["question_id"]: r for r in dataset if r["question_id"] in split[partition]["question_ids"]}
    modes = evaluation_modes(frozen["config"])
    expected = {(qid, mode, repeat) for qid in selected for mode in modes for repeat in range(frozen["config"]["repeats"])}
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
        required = selected[qid].get("necessary_evidence", [])
        response = record.get("response")
        trace = response.get("trace", {}) if response else {}
        record["candidate_ranking"] = _rank_metrics(trace.get("candidate_sources"), required)
        record["reranked_ranking"] = _rank_metrics(trace.get("reranked_sources"), required)
        record["candidate_union_ranking"] = _union_rank_metrics(trace.get("candidate_sources"), required)
        record["reranked_union_ranking"] = _union_rank_metrics(trace.get("reranked_sources"), required)
        record["tree_attribution"] = _tree_attribution(response, required)
        record["b2_attribution"] = _b2_attribution(response, required,
                                                    relation_type=selected[qid].get("relation_type"))
        record["b3_attribution"] = _b3_attribution(response, required,
                                                     relation_type=selected[qid].get("relation_type"))
        record["b15_attribution"] = record["tree_attribution"] if mode == "b15" else None
        record["ablation_attribution"] = _ablation_attribution(response, mode, selected[qid])
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
    pricing = frozen["config"].get("pricing")
    plugins = {mode: _aggregate([r for r in enriched if r["plugin"] == mode], pricing) for mode in modes}
    # Keep the historical b1 tree field while exposing the same explicit
    # extension attribution under the B1.5 name for multi-mode reports.
    for mode in modes:
        if mode == "b15":
            plugins[mode]["b15_attribution"] = plugins[mode].get("tree_attribution")
    questions, families = {}, defaultdict(list)
    paired = dict(a_wins=0, b1_wins=0, ties=0, unknown=0, question_denominator=len(selected))
    for qid, source in selected.items():
        group = [r for r in enriched if r["question_id"] == qid]
        scores = {mode: _aggregate([r for r in group if r["plugin"] == mode], pricing)["success_rate"] for mode in modes}
        deltas = {mode: None if scores[mode] is None or scores["a"] is None else scores[mode] - scores["a"]
                  for mode in modes if mode != "a"}
        question_row = dict(family_id=source["family_id"], **scores)
        question_row.update({f"{mode}_minus_a": delta for mode, delta in deltas.items()})
        questions[qid] = question_row
        for mode, delta in deltas.items():
            families[source["family_id"]].append((mode, delta))
        delta = deltas.get("b1")
        paired["unknown" if delta is None else "b1_wins" if delta > 0 else "a_wins" if delta < 0 else "ties"] += 1
    family_results = {}
    for name, values in families.items():
        by_mode = defaultdict(list)
        for mode, delta in values:
            by_mode[mode].append(delta)
        family_results[name] = dict(questions=len({qid for qid, source in selected.items() if source["family_id"] == name}),
                                    **{f"{mode}_minus_a": None if any(v is None for v in mode_values) else mean(mode_values)
                                       for mode, mode_values in by_mode.items()})
    # Retain a stable b1 family summary even when all b1 values are unknown.
    for name in {source["family_id"] for source in selected.values()}:
        family_results.setdefault(name, dict(questions=sum(source["family_id"] == name for source in selected.values())))
        family_results[name].setdefault("b1_minus_a", None)
    differences = [r["b1_minus_a"] for r in family_results.values() if r.get("b1_minus_a") is not None]
    paired.update(independent_families=len(families),
        family_equal_weighted_difference=mean(differences) if len(differences) == len(families) and differences else None,
        descriptive_family_range=[min(differences), max(differences)] if differences else None,
        uncertainty="descriptive family range only; no confidence interval or adoption claim")
    def ratio(metric):
        if "b1" not in plugins:
            return None
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
        category in selected[r["question_id"]].get("categories", [])], pricing) for mode in modes} for category in categories}
    domains = sorted({source.get("source_domain", source.get("domain")) for source in selected.values()
                      if source.get("source_domain", source.get("domain"))})
    domain_breakdown = {domain: {mode: _aggregate([r for r in enriched if r["plugin"] == mode and
        selected[r["question_id"]].get("source_domain", selected[r["question_id"]].get("domain")) == domain], pricing)
        for mode in modes} for domain in domains}
    relations = sorted({source.get("relation_type") for source in selected.values() if source.get("relation_type")})
    relation_breakdown = {relation: {mode: _aggregate([r for r in enriched if r["plugin"] == mode and
        selected[r["question_id"]].get("relation_type") == relation], pricing)
        for mode in modes} for relation in relations}
    comparisons = {}
    for mode in modes:
        if mode == "a":
            continue
        mode_delta = None if plugins[mode]["success_rate"] is None or plugins["a"]["success_rate"] is None else plugins[mode]["success_rate"] - plugins["a"]["success_rate"]
        mode_values = [questions[qid].get(f"{mode}_minus_a") for qid in questions]
        known = [value for value in mode_values if value is not None]
        comparisons[mode] = {
            "baseline": "a",
            "success_rate": plugins[mode]["success_rate"],
            "a_success_rate": plugins["a"]["success_rate"],
            "success_rate_minus_a": mode_delta,
            "success_rate_delta": mode_delta,
            "question_denominator": len(questions),
            "known_question_differences": len(known),
            "question_difference_mean": mean(known) if known else None,
            "wins": sum(value > 0 for value in known),
            "losses": sum(value < 0 for value in known),
            "ties": sum(value == 0 for value in known),
            "unknown": len(mode_values) - len(known),
        }
    return dict(freeze_id=frozen["freeze_id"], partition=partition, plugins=plugins, paired=paired,
        questions=questions, families=family_results, categories=breakdown,
        domains=domain_breakdown, relations=relation_breakdown,
        comparisons=comparisons, metric_definitions=dict(METRIC_DEFINITIONS),
        missing_execution_rows=len(expected) - len(records),
        gates=dict(measured=measures, checks=checks, passed=False if False in checks.values() else None if None in checks.values() else True),
        decision="exploratory_no_adoption", evidence_kind=sorted({r.get("evidence_kind", "unknown") for r in records}),
        quality_source="external human review only", build_update_cost="not measured by request harness")
