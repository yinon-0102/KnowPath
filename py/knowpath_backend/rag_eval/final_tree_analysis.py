"""Frozen B4 engineering decision: original coordinates, paired families, no score tuning."""
from collections import Counter, defaultdict
from copy import deepcopy
import math
import random
from statistics import mean

from .dataset import digest, numeric
from .costing import navigation_accounting
from .delivery_analysis import (_contract_error, _delivery, _journal, _provider_failed,
                                _quantile, _usage, _valid_sources)
from .scoring import RANK_KS, _rank_metrics, _union_rank_metrics, _union_covered_ids


ANALYSIS_VERSION = 'b4-final-paired-family-v1'
MODES = ('a0', 'f', 'b4')


def _snapshot(record):
    value = (record or {}).get('retrieval_snapshot')
    return value if isinstance(value, dict) else {}


def _trace(record):
    return ((record or {}).get('response') or {}).get('trace') or {}


def _sources(record, field):
    # Only persisted pre-generation context may support the context gate.
    snapshot = _snapshot(record)
    items = snapshot.get(field)
    if field != 'context_sources' and field not in snapshot:
        items = _trace(record).get(field)
    return items if _valid_sources(items) else None


def _coverage(items, required):
    return len(_union_covered_ids(items, required)) / len(required) if items is not None and required else None


def _nonempty(record):
    return bool(record and record.get('service_success') and record.get('answer_status') in {'answered', 'partial'}
                and (record.get('response') or {}).get('claims'))


def _metrics(record, question):
    required = question.get('necessary_evidence') or []
    context = _coverage(_sources(record, 'context_sources'), required)
    citations = ((record or {}).get('response') or {}).get('citations')
    citation = (_coverage(citations, required) if _valid_sources(citations) else None) if _nonempty(record) else 0.0
    values = dict(context_coverage=context, citation_coverage=citation,
                  context_all_necessary=None if context is None else float(context == 1),
                  citation_all_necessary=None if citation is None else float(citation == 1),
                  nonempty_verified_delivery=float(_nonempty(record)),
                  complete_answer=float(bool(_nonempty(record) and record.get('answer_status') == 'answered')))
    for prefix, field in (('candidate', 'candidate_sources'), ('reranked', 'reranked_sources')):
        items = _sources(record, field)
        legacy, union = _rank_metrics(items, required), _union_rank_metrics(items, required)
        for k in RANK_KS:
            values[f'{prefix}_union_recall_at_{k}'] = union['recall_at'][str(k)] if union else None
            values[f'{prefix}_legacy_recall_at_{k}'] = legacy['recall_at'][str(k)] if legacy else None
        values[f'{prefix}_legacy_ndcg_at_10'] = legacy['ndcg_at']['10'] if legacy else None
        values[f'{prefix}_legacy_mrr'] = legacy['mrr'] if legacy else None
        coverage = _coverage(items, required)
        values[f'{prefix}_all_necessary'] = None if coverage is None else float(coverage == 1)
    return values


def _metric_summary(values):
    known = [v for v in values if v is not None]
    return dict(mean=mean(known) if len(known) == len(values) and known else None,
                observed_mean=mean(known) if known else None, measured=len(known),
                unknown=len(values) - len(known), scheduled=len(values))


def _paired(values, questions, baseline, metric, draws):
    families = defaultdict(list)
    for question in questions:
        qid = question['question_id']
        left, right = values[baseline][qid][metric], values['b4'][qid][metric]
        if left is None or right is None:
            return dict(difference=None, question_mean_difference=None, ci95=None,
                        reason='unknown_paired_values', questions=len(questions))
        families[question['family_id']].append(right - left)
    diffs = [mean(families[key]) for key in sorted(families)]
    samples = [sum(diffs[index] for index in indices) / len(diffs) for indices in draws]
    return dict(difference=mean(diffs), question_mean_difference=mean(v for group in families.values() for v in group),
                ci95=[_quantile(samples, .025), _quantile(samples, .975)], families=len(diffs),
                questions=len(questions), reason='seen_development_questions_single_run')


def _navigation(record):
    retrieval = _snapshot(record).get('retrieval', _trace(record).get('retrieval', {})) or {}
    navigation = retrieval.get('navigation')
    return {**retrieval, **navigation} if isinstance(navigation, dict) else retrieval


def _complete_usage(records, *, missing_requests):
    """Merge native navigation only when the physical ledger lacks that stage."""
    usage = _usage(records, missing_requests=missing_requests)
    usage['navigation_disagreements'] = 0
    for record in records:
        nav = navigation_accounting(dict(navigation=_navigation(record), call_journal=_journal(record)))
        usage['navigation_disagreements'] += int(nav['disagreement'])
        if not nav['complete']:
            usage['complete'] = False
        if nav['journal_calls'] or not nav['calls']:
            continue
        extra = _usage([{'call_journal': {'calls': [dict(stage='navigation', usage=c.get('usage'))
                                                    for c in nav['calls']]}}])
        for field in ('observed_input_tokens', 'observed_output_tokens', 'observed_usage_calls', 'unknown_usage_calls'):
            usage[field] += extra[field]
        usage['calls_by_stage']['navigation'] = usage['calls_by_stage'].get('navigation', 0) + len(nav['calls'])
        usage['complete'] = usage['complete'] and extra['complete']
    return usage


def _navigation_calls(record):
    nav = _navigation(record)
    calls = nav.get('calls')
    if isinstance(calls, list):
        return len(calls)
    value = nav.get('navigation_calls', nav.get('call_count'))
    if type(value) is int and value >= 0:
        return value
    calls = (_journal(record) or {}).get('calls')
    if isinstance(calls, list):
        return sum(str(c.get('stage', '')).startswith('navigation') for c in calls)
    return 0 if record and record.get('plugin') == 'a0' else None


def _citation_violations(record):
    """Coordinate provenance only; factual support still requires source review."""
    if not record or not record.get('response'):
        return 0 if record and record.get('attempt_status') != 'unknown' else None
    citations = record['response'].get('citations')
    if citations is None:
        return 0 if not _nonempty(record) else None
    if not _valid_sources(citations):
        return len(citations) if isinstance(citations, list) and citations else 1
    context = _sources(record, 'context_sources')
    if context is None:
        return None
    context_ids = {s.get('chunk_id') for s in context}
    violations = 0
    for citation in citations:
        spans = citation['source_spans']
        identifier = citation.get('chunk_id')
        if ((identifier is not None and identifier not in context_ids)
                or len(_union_covered_ids(context, spans)) != len(spans)):
            violations += 1
    return violations


def _attribution(indexed, values, questions, baseline):
    groups = {name: dict(question_ids=[], citation_difference_sum=0.0)
              for name in ('same_context', 'changed_context', 'unknown_context')}
    per_question = {}
    for question in questions:
        qid = question['question_id']
        left, right = indexed.get((qid, baseline)), indexed.get((qid, 'b4'))
        lh, rh = _snapshot(left).get('context_hash'), _snapshot(right).get('context_hash')
        label = ('same_context' if lh == rh else 'changed_context') if lh and rh else 'unknown_context'
        lc, rc = values[baseline][qid]['citation_coverage'], values['b4'][qid]['citation_coverage']
        diff = rc - lc if lc is not None and rc is not None else None
        groups[label]['question_ids'].append(qid)
        if diff is None:
            groups[label]['citation_difference_sum'] = None
        elif groups[label]['citation_difference_sum'] is not None:
            groups[label]['citation_difference_sum'] += diff
        required = question.get('necessary_evidence') or []
        base_sources, b4_sources = _sources(left, 'candidate_sources'), _sources(right, 'candidate_sources')
        added = None
        if base_sources is not None and b4_sources is not None and required:
            base_ids = {item.get('chunk_id') for item in base_sources}
            new_sources = [item for item in b4_sources if item.get('chunk_id') not in base_ids]
            added = sorted(_union_covered_ids(base_sources + new_sources, required) - _union_covered_ids(base_sources, required))
        nav = _navigation(right)
        baseline_ids = nav.get('baseline_candidate_ids')
        actual_ids = _snapshot(right).get('candidate_ids')
        retained = (set(baseline_ids[:20]) <= set(actual_ids)) if isinstance(baseline_ids, list) and isinstance(actual_ids, list) else nav.get('a_top20_retained')
        per_question[qid] = dict(context_group=label, baseline_context_hash=lh, b4_context_hash=rh,
            citation_difference=diff, added_gold_evidence_indexes=added,
            a_top20_retained=retained, candidate_leaf_count=len(actual_ids) if isinstance(actual_ids, list) else None,
            context_leaf_count=len(_snapshot(right)['context_ids']) if isinstance(_snapshot(right).get('context_ids'), list) else None,
            route_omissions=nav.get('route_omissions', nav.get('omitted_ids')),
            packet_overflow=nav.get('packet_overflow', nav.get('skipped_packets')),
            route_trace=deepcopy(nav))
    for group in groups.values():
        group['count'] = len(group['question_ids'])
        value = group.pop('citation_difference_sum')
        group['citation_contribution'] = value / len(questions) if value is not None else None
    return dict(**groups, per_question=per_question)


def _gate(values, explanation):
    values = list(values)
    return dict(passed=False if False in values else None if any(v is None for v in values) else True,
                checks=values, requirement=explanation)


def _compare(a, b, operation):
    return operation(a, b) if a is not None and b is not None else None


def analyze(records, questions, *, quality_review=None, index_build=None, expected_questions=40,
            seed=20260924, bootstrap_samples=10000):
    """One attempt per question/mode; missing delivery is zero, missing retrieval unknown.

    Threshold gates use unweighted question coverage means (percentage points).
    Paired uncertainty is separately family-equal with frozen bootstrap settings.
    Optional sample/count overrides exist solely for small deterministic test fixtures.
    """
    if len(questions) != expected_questions or expected_questions < 1:
        raise ValueError(f'final protocol requires {expected_questions} questions')
    if type(seed) is not int or type(bootstrap_samples) is not int or bootstrap_samples < 1:
        raise ValueError('invalid bootstrap configuration')
    gold = {q['question_id']: q for q in questions}
    if len(gold) != len(questions):
        raise ValueError('duplicate question')
    indexed = {}
    for record in records:
        key = record['question_id'], record['plugin']
        if key[0] not in gold or key[1] not in MODES or record.get('repeat') != 0:
            raise ValueError('unscheduled record')
        if key in indexed:
            raise ValueError('duplicate record')
        indexed[key] = record
    values = {mode: {qid: _metrics(indexed.get((qid, mode)), q) for qid, q in gold.items()} for mode in MODES}
    metric_names = tuple(values['a0'][next(iter(gold))])
    family_count = len({q['family_id'] for q in questions})
    rng = random.Random(seed)
    draws = [rng.choices(range(family_count), k=family_count) for _ in range(bootstrap_samples)]
    comparisons = {f'b4-{base}': {metric: _paired(values, questions, base, metric, draws)
                    for metric in metric_names} for base in ('a0', 'f')}
    summaries = {}
    for mode in MODES:
        actual = [indexed[qid, mode] for qid in gold if (qid, mode) in indexed]
        missing = expected_questions - len(actual)
        unknown = sum(r.get('attempt_status') == 'unknown' for r in actual)
        failures = sum(not r.get('service_success') and r.get('attempt_status') != 'unknown' for r in actual)
        latency = [r['latency_ms'] for r in actual if numeric(r.get('latency_ms'))]
        costs = [r['cost'] for r in actual if numeric(r.get('cost'))]
        statuses = Counter(r.get('answer_status') if r.get('service_success') else
                           ('unknown' if r.get('attempt_status') == 'unknown' else 'failed') for r in actual)
        statuses['missing'] = missing
        metrics = {metric: _metric_summary([v[metric] for v in values[mode].values()]) for metric in metric_names}
        usage = _complete_usage(actual, missing_requests=missing + unknown)
        usage['mean_input_tokens'] = usage['observed_input_tokens'] / expected_questions if usage['complete'] else None
        usage['mean_output_tokens'] = usage['observed_output_tokens'] / expected_questions if usage['complete'] else None
        usage['embedding_calls'] = sum(count for stage, count in usage['calls_by_stage'].items() if stage == 'embedding')
        usage['rerank_calls'] = sum(count for stage, count in usage['calls_by_stage'].items() if stage in {'rerank', 'reranking'})
        usage['chat_calls'] = sum(count for stage, count in usage['calls_by_stage'].items()
                                  if stage in {'generation', 'verification', 'revision'} or stage.startswith('navigation'))
        nav_calls = [_navigation_calls(r) for r in actual]
        nav_latency = [_navigation(r).get('navigation_latency_ms', _navigation(r).get('elapsed_ms')) for r in actual]
        nav_latency = [(_navigation(r).get('elapsed_seconds') * 1000
                        if numeric(_navigation(r).get('elapsed_seconds')) and value is None
                        else value) for r, value in zip(actual, nav_latency)]
        nav_latency = [v for v in nav_latency if numeric(v)]
        violations = [_citation_violations(r) for r in actual]
        terminal = [r for r in actual if _delivery(r).get('reason') in {'generation_contract_failed', 'verification_contract_failed'}]
        summaries[mode] = dict(scheduled=expected_questions, attempted=len(actual), missing=missing,
            unknown=unknown, completed=len(actual) - unknown, failed=failures,
            nonempty_verified_delivery=sum(_nonempty(r) for r in actual),
            full_answer_count=sum(_nonempty(r) and r.get('answer_status') == 'answered' for r in actual),
            complete_answer_rate=metrics['complete_answer']['mean'], status_counts=dict(statuses), metrics=metrics,
            all_necessary_counts={prefix: sum(v[prefix + '_all_necessary'] == 1 for v in values[mode].values())
                                  for prefix in ('candidate', 'reranked', 'context', 'citation')},
            contract_error_requests=sum(_contract_error(r) for r in actual),
            contract_empty_answers=sum(not (r.get('response') or {}).get('claims') for r in terminal),
            recovered_partial=sum(_delivery(r).get('recovered') is True for r in actual),
            provider_failure_requests=sum(_provider_failed(r) for r in actual),
            error_categories=dict(Counter(r.get('error_code') for r in actual if r.get('error_code'))),
            source_violations=sum(violations) if not missing and all(v is not None for v in violations) else None,
            latency_ms=dict(p50=_quantile(latency, .5), p95=_quantile(latency, .95), measured=len(latency),
                            complete=len(latency) == expected_questions),
            navigation=dict(calls=sum(v for v in nav_calls if v is not None),
                call_budget_ok=all(v <= 2 for v in nav_calls if v is not None) if len(nav_calls) == expected_questions and all(v is not None for v in nav_calls) else None,
                latency_ms=dict(p50=_quantile(nav_latency, .5), p95=_quantile(nav_latency, .95), measured=len(nav_latency)),
                fallback_requests=sum(bool(_navigation(r).get('fallback')) for r in actual),
                failure_requests=sum(bool(_navigation(r).get('failure_reason') or _navigation(r).get('navigation_error')
                    or any(c.get('failure') for c in _navigation(r).get('calls', []) if isinstance(c, dict))) for r in actual)),
            usage=usage, quality_correctness=None,
            cost=dict(total=sum(costs) if len(costs) == expected_questions else None,
                      observed_subtotal=sum(costs) if costs else None, unknown=expected_questions - len(costs)))
    attribution = {f'b4-{base}': _attribution(indexed, values, questions, base) for base in ('a0', 'f')}
    changed = set()
    for qid in gold:
        for base in ('a0', 'f'):
            if any(values['b4'][qid][metric] != values[base][qid][metric] for metric in metric_names):
                changed.add(qid)
            b4, baseline = indexed.get((qid, 'b4')), indexed.get((qid, base))
            if (b4 or {}).get('error_code') != (baseline or {}).get('error_code'):
                changed.add(qid)
    review = quality_review or {}
    reviewed = set(review.get('reviewed_question_ids') or [])
    review_complete = (review.get('status') == 'completed' and review.get('reviewer_type') == 'human'
                       and review.get('blinded') is True and changed <= reviewed)
    quality = (review.get('serious_new_errors') == 0 and review.get('unreliable_coverage_gains') is False) if review_complete else None
    b4 = summaries['b4']
    gates = {}
    for metric, threshold in (('context_coverage', .03), ('citation_coverage', .05)):
        gates[metric] = _gate((_compare(b4['metrics'][metric]['mean'], summaries[base]['metrics'][metric]['mean'],
                                      lambda b, a: b - a >= threshold - 1e-12) for base in ('a0', 'f')),
                              f'B4 minus A0 and F each >= {threshold:.0%}; all scheduled questions')
    benefits = set(review.get('genuine_context_benefit_question_ids') or [])
    attribution_checks = []
    for base in ('a0', 'f'):
        part = attribution[f'b4-{base}']
        contribution = part['changed_context']['citation_contribution']
        attribution_checks.extend([contribution > 0 if contribution is not None else None,
            len(benefits & set(part['changed_context']['question_ids'])) >= 2 if review_complete else None])
    gates['context_attribution'] = _gate(attribution_checks,
        'changed-context citation contribution positive against both controls; >=2 blinded source-confirmed benefits')
    negative_ids = [q['question_id'] for q in questions if q.get('relation_type') in
                    {'single_leaf', 'adjacency_negative', 'adjacent_noise'} or q.get('tree_label') == 'negative']
    negative = {}
    for mode in MODES:
        negative[mode] = _metric_summary([values[mode][qid]['context_coverage'] for qid in negative_ids])
    original_labels_complete = len(negative_ids) == 18 if expected_questions == 40 else bool(negative_ids)
    gates['ranking_and_local_tasks'] = _gate([
        _compare(b4['metrics']['reranked_legacy_ndcg_at_10']['mean'], summaries['a0']['metrics']['reranked_legacy_ndcg_at_10']['mean'], lambda b, a: b - a >= -.01 - 1e-12),
        _compare(negative['b4']['mean'], negative['a0']['mean'], lambda b, a: b - a >= -.01 - 1e-12) if original_labels_complete else None,
        review.get('serious_new_errors') == 0 if review_complete else None],
        'NDCG@10 and frozen 18 negative-label context coverage decline <=1pp; no new severe source/fact errors')
    delivery_checks = []
    for base in ('a0', 'f'):
        complete = not any(summaries[m]['missing'] or summaries[m]['unknown'] for m in ('b4', base))
        for field, operation in (('nonempty_verified_delivery', lambda b, a: b >= a),
                                 ('contract_empty_answers', lambda b, a: b <= a), ('failed', lambda b, a: b <= a)):
            delivery_checks.append(operation(b4[field], summaries[base][field]) if complete else None)
    delivery_checks.append(b4['source_violations'] == 0 if b4['source_violations'] is not None else None)
    gates['delivery_reliability'] = _gate(delivery_checks, 'verified nonempty >= both; empty contracts/failures <= both; source violations zero')
    gates['latency'] = _gate([_compare(b4['latency_ms']['p95'], summaries['a0']['latency_ms']['p95'],
        lambda b, a: b <= 1.2 * a) if all(summaries[m]['latency_ms']['complete'] for m in ('a0', 'b4')) else None], 'B4 total P95 <= A0 x1.2, including failures and fallbacks')
    gates['online_cost_proxy'] = _gate([
        *[_compare(b4['usage'][field], summaries['a0']['usage'][field], lambda b, a: b <= 1.5 * a)
          for field in ('mean_input_tokens', 'mean_output_tokens')],
        summaries['f']['navigation']['call_budget_ok'], b4['navigation']['call_budget_ok']],
        'mean input/output tokens separately <= A0 x1.5; F/B4 each <=2 navigation calls per request')
    gates['quality_review'] = _gate([quality], 'all score changes/new errors reviewed blinded against source; no unreliable coverage gain')
    complete = all(not summary['missing'] and not summary['unknown'] for summary in summaries.values())
    provider_failures = sum(summary['provider_failure_requests'] for summary in summaries.values())
    no_context = all(summary['metrics']['context_coverage']['measured'] == 0 for summary in summaries.values())
    missing_context = any(summary['metrics']['context_coverage']['unknown'] for summary in summaries.values())
    delivery_outage = any(summary['attempted'] == expected_questions and summary['failed'] == expected_questions
                          and summary['provider_failure_requests'] == expected_questions for summary in summaries.values())
    unknown_budget = any(not summary['usage']['complete'] for summary in summaries.values())
    unknown_ranking = any(summary['metrics']['reranked_legacy_ndcg_at_10']['unknown'] for summary in summaries.values())
    validity_reasons = []
    if not complete:
        validity_reasons.append('incomplete_or_unknown_attempts')
    if missing_context:
        validity_reasons.append('missing_pre_generation_context')
    if unknown_ranking:
        validity_reasons.append('missing_ranking_observations')
    if unknown_budget:
        validity_reasons.append('incomplete_usage_accounting')
    experiment_validity = dict(status='valid', reason='all scheduled modes have usable independent observations')
    if delivery_outage or provider_failures and no_context:
        experiment_validity = dict(status='invalid_infrastructure', reason='provider outage invalidates retrieval or delivery comparison')
    elif validity_reasons:
        experiment_validity = dict(status='unknown_prerequisites', reason='; '.join(validity_reasons))
    experiment_validity['unknown_prerequisites'] = validity_reasons
    passed = all(gate['passed'] is True for gate in gates.values())
    if experiment_validity['status'] != 'valid' or not complete:
        decision = 'unknown'
    elif passed:
        decision = 'retain_controlled_optional'
    elif any(gate['passed'] is False for gate in gates.values()):
        decision = 'drop_online_tree'
    else:
        decision = 'unknown'
    return dict(version=ANALYSIS_VERSION, seed=seed, bootstrap_samples=bootstrap_samples,
        confidence=.95, weighting='equal_family', independent_questions=expected_questions,
        independent_families=family_count, repeats=1, scheduled_requests=expected_questions * 3,
        actual_requests=len(records), modes=summaries, comparisons=comparisons, per_question=values,
        attribution=attribution, negative_label_question_ids=negative_ids, negative_label_metrics=negative,
        structural_audit_question_ids=[qid for qid in gold if qid.endswith(('03', '10'))],
        quality_review=dict(status='completed' if review_complete else 'pending', correctness=quality,
            required_question_ids=sorted(changed), reviewed_question_ids=sorted(reviewed),
            reviewer_type=review.get('reviewer_type'), blinded=review.get('blinded')),
        gates=gates, decision=decision, default_mode='a0', further_tree_iteration=False,
        monetary_gate='unknown' if any(s['cost']['total'] is None for s in summaries.values()) else 'not_a_frozen_gate',
        experiment_validity=experiment_validity,
        index_build=deepcopy(index_build), input_sha256=digest(records),
        note='Single run of seen development questions; coordinate coverage is not correctness. '
             'Intervals do not estimate within-question generation variance. Unknown review prevents adoption.')
