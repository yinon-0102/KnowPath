from copy import deepcopy

import pytest

from knowpath_backend.rag_eval.delivery_analysis import analyze


SPAN = dict(material_version_id='m', artifact_hash='h', page=1, block=0, start=0, end=10)


def question(qid='q1', family='f1'):
    return dict(question_id=qid, family_id=family, necessary_evidence=[SPAN])


def record(mode, qid='q1', repeat=0, covered=True, success=True):
    sources = [dict(chunk_id='c', source_spans=[SPAN])] if covered else []
    return dict(question_id=qid, family_id='f1', plugin=mode, repeat=repeat,
        service_success=success, answer_status='partial' if success else None,
        latency_ms=100, cost=None, response=dict(status='partial', claims=[dict(text='verified')],
        sources=sources, citations=sources, trace=dict(candidate_sources=sources,
        reranked_sources=sources, delivery=dict(reason='semantic_result', recovered=False),
        call_journal=dict(calls=[]))) if success else None)


def test_equal_paired_results_and_repeat_units():
    rows = [record(m, repeat=r) for m in ('a', 'b3_unit') for r in range(3)]
    out = analyze(rows, [question()], repeats=3, modes=('a', 'b3_unit'), bootstrap_samples=100)
    pair = out['comparisons']['b3_unit-a']['context_coverage']
    assert pair['difference'] == 0
    assert pair['ci95'] == [0, 0]
    assert pair['families'] == 1
    assert out['modes']['a']['scheduled'] == 3
    assert out['modes']['a']['metrics']['candidate_union_recall_at_40']['mean'] == 1


def test_failed_and_missing_delivery_zero_but_retrieval_unknown():
    rows = [record('a', success=False), record('b3_unit')]
    out = analyze(rows, [question()], repeats=2, modes=('a', 'b3_unit'), bootstrap_samples=100)
    a = out['modes']['a']
    assert a['missing'] == 1 and a['failed'] == 1
    assert a['metrics']['context_coverage']['mean'] == 0
    assert a['metrics']['candidate_union_recall_at_40']['mean'] is None
    assert a['metrics']['candidate_union_recall_at_40']['unknown'] == 2
    assert a['cost']['total'] is None
    assert a['quality_correctness'] is None


def test_hand_calculated_paired_difference_reproducible():
    questions = [question('q1', 'f1'), question('q2', 'f2')]
    rows = [record('a', qid='q1', covered=False), record('a', qid='q2'),
            record('b3_unit', qid='q1'), record('b3_unit', qid='q2')]
    kwargs = dict(repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=1000)
    one = analyze(rows, questions, **kwargs)
    assert one == analyze(rows, questions, **kwargs)
    assert one['comparisons']['b3_unit-a']['context_coverage']['difference'] == .5
    reverse = [dict(r, plugin='b3_unit' if r['plugin'] == 'a' else 'a') for r in rows]
    assert analyze(reverse, questions, **kwargs)['comparisons']['b3_unit-a']['context_coverage']['difference'] == -.5


def test_duplicate_and_unscheduled_keys_rejected():
    r = record('a')
    with pytest.raises(ValueError, match='duplicate'):
        analyze([r, deepcopy(r)], [question()], repeats=1, modes=('a', 'b3_unit'))
    with pytest.raises(ValueError, match='unscheduled'):
        analyze([dict(r, repeat=2)], [question()], repeats=1, modes=('a', 'b3_unit'))


def test_restored_partial_still_counts_contract_error_and_tokens():
    r = record('a')
    r['response']['trace']['delivery'] = dict(reason='verification_contract_failed', recovered=True)
    r['response']['trace']['call_journal']['calls'] = [dict(stage='verification',
        validation='failed', schema_rule='schema_fields', usage=dict(prompt_tokens=20, completion_tokens=5))]
    out = analyze([r], [question()], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=100)
    a = out['modes']['a']
    assert a['recovered_partial'] == 1 and a['contract_error_requests'] == 1
    assert a['nonempty_verified_delivery'] == 1 and a['contract_empty_answers'] == 0
    assert a['status_counts']['partial'] == 1
    assert a['usage']['observed_input_tokens'] == 20
    assert a['usage']['observed_output_tokens'] == 5


def test_failed_journal_counts_without_success_response():
    r = record('a', success=False)
    r['call_journal'] = dict(calls=[dict(stage='generation', validation='failed',
        schema_rule='schema_fields', usage={})])
    out = analyze([r], [question()], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=100)
    a = out['modes']['a']
    assert a['contract_error_requests'] == 1
    assert a['usage']['unknown_usage_calls'] == 1
    assert a['latency_ms']['p95'] == 100


def test_success_with_missing_coverage_trace_stays_unknown():
    r = record('a')
    del r['response']['sources']
    out = analyze([r], [question()], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=100)
    assert out['modes']['a']['metrics']['context_coverage']['mean'] is None
    assert out['comparisons']['b3_unit-a']['context_coverage']['difference'] is None


@pytest.mark.parametrize('payload', [{}, [{}], ['bad'], [dict(chunk_id='c', source_spans=[{}])]])
def test_malformed_retrieval_is_unknown(payload):
    r = record('a')
    r['response']['trace']['candidate_sources'] = payload
    out = analyze([r], [question()], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=20)
    metric = out['modes']['a']['metrics']['candidate_union_recall_at_40']
    assert metric['mean'] is None and metric['unknown'] == 1


def test_nonbillable_journal_is_not_unknown_usage():
    r = record('a')
    r['response']['trace']['call_journal']['calls'] = [
        dict(stage='retrieval', billing_status='not_applicable', usage={}),
        dict(stage='generation', billing_status='usage_observed', usage=dict(input_tokens=20, output_tokens=5))]
    usage = analyze([r], [question()], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=20)['modes']['a']['usage']
    assert usage['complete'] is True
    assert usage['unknown_usage_calls'] == 0
    assert usage['calls_by_stage']['retrieval'] == 1


def test_missing_planned_requests_prevent_complete_usage():
    usage = analyze([], [question()], repeats=3, modes=('a', 'b3_unit'), bootstrap_samples=20)['modes']['a']['usage']
    assert usage['complete'] is False
    assert usage['missing_requests'] == 3


def test_text_source_nullable_page_is_valid_coordinate():
    q, r = deepcopy(question()), deepcopy(record('a'))
    q['necessary_evidence'][0]['page'] = None
    for items in (r['response']['sources'], r['response']['citations'],
                  r['response']['trace']['candidate_sources'], r['response']['trace']['reranked_sources']):
        items[0]['source_spans'][0]['page'] = None
    out = analyze([r], [q], repeats=1, modes=('a', 'b3_unit'), bootstrap_samples=20)
    assert out['modes']['a']['metrics']['context_coverage']['mean'] == 1


def test_provider_failure_not_hidden_by_later_service_completion():
    r=record('a')
    r['response']['trace']['call_journal']['calls']=[dict(stage='generation',
        status='failed',failure_kind='server_error',usage={})]
    out=analyze([r],[question()],repeats=1,modes=('a','b3_unit'),bootstrap_samples=20)
    assert out['modes']['a']['provider_failure_requests']==1
    assert out['modes']['a']['service_successes']==1


def test_legacy_and_union_metrics_are_not_redefined():
    from knowpath_backend.rag_eval.scoring import _rank_metrics, _union_rank_metrics
    r=record('a')
    expected=_rank_metrics(r['response']['trace']['reranked_sources'],[SPAN])
    out=analyze([r],[question()],repeats=1,modes=('a','b3_unit'),bootstrap_samples=20)
    assert out['modes']['a']['metrics']['reranked_legacy_ndcg_at_10']['mean']==expected['ndcg_at']['10']
