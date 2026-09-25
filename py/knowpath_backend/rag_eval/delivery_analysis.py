"""Frozen exploratory delivery analysis; coordinate coverage is not correctness."""
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
import random
from statistics import mean

from .scoring import RANK_KS, _rank_metrics, _union_rank_metrics, evidence_coverage

ANALYSIS_VERSION = 'delivery-paired-family-v1'


def _quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _valid_sources(items):
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('source_spans'), list) or not item['source_spans']:
            return False
        for span in item['source_spans']:
            if (not isinstance(span, dict) or any(k not in span for k in
                    ('material_version_id', 'artifact_hash', 'page', 'block'))
                    or any(not isinstance(span[k], str) or not span[k] for k in ('material_version_id', 'artifact_hash'))
                    or (span['page'] is not None and type(span['page']) is not int)
                    or (span['block'] is not None and type(span['block']) not in (str, int))
                    or type(span.get('start')) is not int or type(span.get('end')) is not int
                    or not 0 <= span['start'] < span['end']):
                return False
    return True


def _metrics(record, gold):
    response = record.get('response') if record else None
    delivered = bool(record and record.get('service_success') and response)
    trace = (response or {}).get('trace', {})
    values = {name: (evidence_coverage(response, gold, field) if _valid_sources(response.get(field)) else None) if delivered else 0.0
              for name, field in (('context_coverage', 'sources'), ('citation_coverage', 'citations'))}
    for prefix, field in (('candidate', 'candidate_sources'), ('reranked', 'reranked_sources')):
        items = trace.get(field)
        if not _valid_sources(items):
            items = None
        union = _union_rank_metrics(items, gold.get('necessary_evidence', []))
        legacy = _rank_metrics(items, gold.get('necessary_evidence', []))
        for k in RANK_KS:
            values[f'{prefix}_union_recall_at_{k}'] = union['recall_at'][str(k)] if union else None
            values[f'{prefix}_legacy_recall_at_{k}'] = legacy['recall_at'][str(k)] if legacy else None
            values[f'{prefix}_legacy_ndcg_at_{k}'] = legacy['ndcg_at'][str(k)] if legacy else None
        values[f'{prefix}_legacy_mrr'] = legacy['mrr'] if legacy else None
    return values


def _journal(record):
    if not record:
        return None
    trace = (record.get('response') or {}).get('trace', {})
    value = trace.get('call_journal', record.get('call_journal'))
    return value if isinstance(value, dict) and isinstance(value.get('calls'), list) else None


def _delivery(record):
    return ((record or {}).get('response') or {}).get('trace', {}).get('delivery', {})


def _contract_error(record):
    journal = _journal(record) or {}
    repairs = ((record or {}).get('response') or {}).get('trace', {}).get('contract_repairs', [])
    return (any(c.get('validation') == 'failed' and c.get('schema_rule') for c in journal.get('calls', []))
            or bool(repairs) or _delivery(record).get('reason') in
            {'generation_contract_failed', 'verification_contract_failed'})


def _provider_failed(record):
    failures={'connect_timeout','read_timeout','write_timeout','pool_timeout','network_error',
              'authentication','rate_limited','server_error','http_error','response_json',
              'response_shape','content_json','content_shape','finish_reason','response_refused'}
    return any(call.get('failure_kind') in failures for call in (_journal(record) or {}).get('calls',[]))


def _paired(question_values, questions, mode, metric, *, seed, samples):
    families = defaultdict(list)
    for q in questions:
        key = q['question_id']
        left, right = question_values['a'][key][metric], question_values[mode][key][metric]
        if left is None or right is None:
            return dict(difference=None, ci95=None, families=None, reason='unknown_paired_values')
        families[q['family_id']].append(right - left)
    diffs = [mean(families[key]) for key in sorted(families)]
    rng = random.Random(seed)
    draws = [mean(rng.choices(diffs, k=len(diffs))) for _ in range(samples)]
    return dict(difference=mean(diffs), ci95=[_quantile(draws, .025), _quantile(draws, .975)],
                families=len(diffs), questions=len(questions), reason='exploratory_seen_questions')


def _usage(records, *, missing_requests=0):
    counts = Counter()
    input_tokens = output_tokens = observed_calls = unknown_calls = unknown_journals = 0
    for record in records:
        journal = _journal(record)
        if journal is None:
            unknown_journals += 1
            continue
        for call in journal['calls']:
            stage = call.get('stage', 'unknown')
            counts[stage] += 1
            if call.get('billing_status') == 'not_applicable':
                continue
            usage = call.get('usage') or {}
            inp = usage.get('input_tokens', usage.get('prompt_tokens'))
            out = usage.get('output_tokens', usage.get('completion_tokens'))
            if stage in {'embedding', 'reranking'}:
                inp = inp if inp is not None else usage.get('total_tokens')
                out = 0
            known = all(type(v) is int and v >= 0 for v in (inp, out))
            if known:
                input_tokens += inp
                output_tokens += out
                observed_calls += 1
            else:
                unknown_calls += 1
    return dict(calls_by_stage=dict(counts), observed_input_tokens=input_tokens,
                observed_output_tokens=output_tokens, observed_usage_calls=observed_calls,
                unknown_usage_calls=unknown_calls, unknown_journals=unknown_journals,
                missing_requests=missing_requests,
                complete=unknown_calls == 0 and unknown_journals == 0 and missing_requests == 0)


def analyze(records, questions, *, repeats=3, modes=('a', 'b2_r1', 'b3_unit'),
            seed=20260923, bootstrap_samples=10000):
    """Aggregate repeats per question, then bootstrap paired family means.

    Planned failed/missing delivery is zero. Missing retrieval observations and
    malformed successful traces remain unknown, never invented zero retrieval.
    """
    if not questions or type(repeats) is not int or repeats < 1 or bootstrap_samples < 1:
        raise ValueError('invalid analysis schedule')
    if len(set(modes)) != len(modes) or 'a' not in modes:
        raise ValueError('invalid comparison modes')
    gold = {q['question_id']: q for q in questions}
    if len(gold) != len(questions):
        raise ValueError('duplicate question')
    scheduled = {(q, m, r) for q in gold for m in modes for r in range(repeats)}
    indexed = {}
    for record in records:
        key = (record['question_id'], record['plugin'], record['repeat'])
        if key not in scheduled:
            raise ValueError('unscheduled record')
        if key in indexed:
            raise ValueError('duplicate record')
        indexed[key] = record
    per_request = {key: _metrics(indexed.get(key), gold[key[0]]) for key in sorted(scheduled)}
    metric_names = tuple(next(iter(per_request.values())))
    question_values = {m: {} for m in modes}
    summaries = {}
    for mode in modes:
        keys = sorted(key for key in scheduled if key[1] == mode)
        actual = [indexed[key] for key in keys if key in indexed]
        for qid in gold:
            question_values[mode][qid] = {}
            for metric in metric_names:
                vals = [per_request[qid, mode, r][metric] for r in range(repeats)]
                question_values[mode][qid][metric] = mean(vals) if all(v is not None for v in vals) else None
        metrics = {}
        for metric in metric_names:
            values = [per_request[key][metric] for key in keys]
            known_questions = [v[metric] for v in question_values[mode].values() if v[metric] is not None]
            metrics[metric] = dict(mean=mean(known_questions) if known_questions else None,
                measured=sum(v is not None for v in values), unknown=sum(v is None for v in values),
                complete_questions=len(known_questions), scheduled=len(keys))
        successes = sum(bool(r.get('service_success')) for r in actual)
        nonempty = sum(bool(r.get('service_success') and (r.get('response') or {}).get('claims')
                            and r.get('answer_status') in {'answered', 'partial'}) for r in actual)
        recovered = sum(_delivery(r).get('recovered') is True and r.get('answer_status') == 'partial' for r in actual)
        terminal = [r for r in actual if _delivery(r).get('reason') in
                    {'generation_contract_failed', 'verification_contract_failed'}]
        costs = [r.get('cost') for r in actual]
        known_costs = [v for v in costs if type(v) in (int, float) and math.isfinite(v) and v >= 0]
        latency = [r['latency_ms'] for r in actual if type(r.get('latency_ms')) in (int, float)
                   and math.isfinite(r['latency_ms']) and r['latency_ms'] >= 0]
        summaries[mode] = dict(scheduled=len(keys), attempted=len(actual), missing=len(keys)-len(actual),
            failed=len(actual)-successes, service_successes=successes, service_completion_rate=successes/len(keys),
            status_counts=dict(Counter(r.get('answer_status') if r.get('service_success') else 'failed' for r in actual)),
            nonempty_verified_delivery=nonempty, nonempty_verified_delivery_rate=nonempty/len(keys),
            contract_error_requests=sum(bool(_contract_error(r)) for r in actual),
            provider_failure_requests=sum(_provider_failed(r) for r in actual),
            terminal_contract_fallbacks=len(terminal), recovered_partial=recovered,
            contract_empty_answers=sum(not (r.get('response') or {}).get('claims') for r in terminal),
            metrics=metrics, quality_correctness=None,
            usage=_usage(actual, missing_requests=len(keys)-len(actual)), cost=dict(total=sum(known_costs) if len(known_costs)==len(keys) else None,
                observed_subtotal=sum(known_costs) if known_costs else None,
                measured=len(known_costs), unknown=len(keys)-len(known_costs)),
            latency_ms=dict(p50=_quantile(latency,.5),p95=_quantile(latency,.95),measured=len(latency),
                            complete=len(latency)==len(keys)))
    comparisons = {f'{mode}-a': {metric: _paired(question_values,questions,mode,metric,seed=seed,
                   samples=bootstrap_samples) for metric in metric_names} for mode in modes if mode != 'a'}
    for mode in modes:
        n=summaries[mode]['scheduled']
        summaries[mode]['contract_error_rate']=summaries[mode]['contract_error_requests']/n
        summaries[mode]['terminal_contract_fallback_rate']=summaries[mode]['terminal_contract_fallbacks']/n
    return dict(version=ANALYSIS_VERSION, seed=seed, bootstrap_samples=bootstrap_samples,
        confidence=.95, independent_questions=len(questions), independent_families=len({q['family_id'] for q in questions}),
        repeats=repeats, scheduled_requests=len(scheduled), actual_requests=len(records),
        note='Exploratory seen-development-question intervals; citation coordinates and status are not correctness.',
        modes=summaries, comparisons=comparisons, per_question=question_values,
        input_sha256=sha256(json.dumps(records,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
