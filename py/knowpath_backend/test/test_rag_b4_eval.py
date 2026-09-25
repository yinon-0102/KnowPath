"""B4 frozen runs preserve attempts, retrieval evidence and honest denominators."""
from copy import deepcopy
import json

import pytest

from knowpath_backend.rag_eval.dataset import digest, evaluation_modes
from knowpath_backend.rag_eval import runner


SPAN = dict(material_version_id='mv', artifact_hash='hash', page=1, block='b', start=0, end=10)
SOURCE = dict(chunk_id='leaf', source_spans=[SPAN])
MODES = ('a0', 'f', 'b4')


def question(qid='q1', family='family'):
    return dict(question_id=qid, family_id=family, question='Q', scope_snapshot_id='scope',
                necessary_evidence=[SPAN], tree_label='negative')


def record(mode, qid='q1', covered=True, success=True, context_hash=None):
    sources = [deepcopy(SOURCE)] if covered else []
    snapshot = dict(stage='context', candidate_ids=['leaf'], candidate_sources=sources,
                    reranked_sources=sources, context_sources=sources,
                    context_hash=context_hash or ('yes' if covered else 'no'),
                    retrieval=dict(navigation_calls=0, navigation_latency_ms=0,
                                   baseline_candidate_ids=['leaf'], a_top20_retained=True))
    response = dict(status='answered', claims=[dict(text='verified')], citations=sources,
                    trace=dict(call_journal=dict(calls=[]), delivery=dict(reason='semantic_result')))
    return dict(question_id=qid, plugin=mode, repeat=0, service_success=success,
                answer_status='answered' if success else None, latency_ms=100,
                response=response if success else None, retrieval_snapshot=snapshot,
                call_journal=dict(calls=[]), cost=None)


@pytest.fixture
def harness(monkeypatch):
    binding = dict(space_id='space', scope_snapshot_id='scope', expected_scope_version=1,
        expected_bindings=[dict(material_id='m', material_version_id='mv', graph_version=1)],
        expected_manifest_ids=['manifest'], expected_retrieval_versions=['rv'],
        manifest_configuration_hashes={'manifest': digest({'test': 1})})
    config = dict(modes=list(MODES), repeats=1, pricing={},
                  runtime_bindings={'scope': binding})
    rows = [question(f'q{i:02}', f'f{i // 2}') for i in range(40)]
    frozen = dict(freeze_id='frozen', config=config)
    monkeypatch.setattr(runner, 'verify_freeze', lambda _: (frozen, rows, {}))
    monkeypatch.setattr(runner, 'authorize_partition', lambda *args: rows)
    actual = dict(space_id='space', scope_snapshot_id='scope', scope_version=1,
                  bindings=binding['expected_bindings'], manifests=[dict(manifest_id='manifest',
                  retrieval_version_id='rv', configuration={'test': 1})])
    def resolver(_): return deepcopy(actual)
    class Pipeline:
        def __init__(self, mode): self.mode = mode
        def answer(self, *args, **kwargs):
            self.last_retrieval_snapshot = record(self.mode)['retrieval_snapshot']
            callback = getattr(self, 'retrieval_snapshot_callback', None)
            if callback: callback(deepcopy(self.last_retrieval_snapshot))
            return dict(status='answered', claims=[{'text': 'verified'}], trace=dict(
                scope_snapshot_id='scope', manifest_ids=['manifest'], retrieval_versions=['rv']))
    return Pipeline, resolver


def test_modes_and_rotating_120_durable_attempts(harness, tmp_path):
    assert evaluation_modes({'modes': list(MODES)}) == MODES
    factory, resolver = harness
    journal = tmp_path / 'attempts.jsonl'
    calls = []
    class Checked(factory):
        def answer(self, *args, **kwargs):
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            assert events[-1]['event'] == 'started'
            calls.append(self.mode)
            return super().answer(*args, **kwargs)
    rows = runner.run('freeze', tmp_path / 'records.jsonl', journal_path=journal,
                      pipeline_factory=Checked, scope_resolver=resolver)
    assert len(rows) == 120
    assert calls[:9] == ['a0', 'f', 'b4', 'f', 'b4', 'a0', 'b4', 'a0', 'f']
    assert all(len(r['record_hash']) == 64 for r in rows)
    assert sum(json.loads(line)['event'] == 'terminal' for line in journal.read_text().splitlines()) == 120
    def forbidden(_): raise AssertionError('completed key resent')
    assert runner.run('freeze', tmp_path / 'records.jsonl', journal_path=journal, resume=True,
                      pipeline_factory=forbidden, scope_resolver=resolver) == rows


def test_crashed_started_is_unknown_never_resent_and_snapshot_survives(harness, tmp_path):
    factory, resolver = harness
    journal, output = tmp_path / 'attempts.jsonl', tmp_path / 'records.jsonl'
    class Crash(factory):
        def answer(self, *args, **kwargs):
            super().answer(*args, **kwargs)
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runner.run('freeze', output, journal_path=journal, pipeline_factory=Crash, scope_resolver=resolver)
    calls = []
    class Observe(factory):
        def answer(self, *args, **kwargs):
            calls.append(self.mode)
            return super().answer(*args, **kwargs)
    rows = runner.run('freeze', output, journal_path=journal, resume=True,
                      pipeline_factory=Observe, scope_resolver=resolver)
    assert len(calls) == 119 and calls[0] == 'f'
    assert rows[0]['attempt_status'] == 'unknown'
    assert rows[0]['error_code'] == 'EVALUATION_ATTEMPT_UNKNOWN'
    assert rows[0]['retrieval_snapshot']['context_sources'] == [SOURCE]


def test_resume_rejects_record_tampering(harness, tmp_path):
    factory, resolver = harness
    journal, output = tmp_path / 'attempts.jsonl', tmp_path / 'records.jsonl'
    def stop(_): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runner.run('freeze', output, journal_path=journal, pipeline_factory=factory,
                   scope_resolver=resolver, after_record=stop)
    row = json.loads(output.read_text())
    row['latency_ms'] += 1
    output.write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError, match='hash|prefix'):
        runner.run('freeze', output, journal_path=journal, resume=True,
                   pipeline_factory=factory, scope_resolver=resolver)


def test_failure_retains_independent_context_and_missing_delivery_denominator():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    out = analyze([record('a0', success=False)], [question()], expected_questions=1,
                  bootstrap_samples=50)
    a = out['modes']['a0']
    assert a['metrics']['context_coverage']['mean'] == 1
    assert a['metrics']['citation_coverage']['mean'] == 0
    assert a['metrics']['candidate_union_recall_at_40']['mean'] == 1
    assert out['modes']['f']['metrics']['context_coverage']['mean'] is None
    assert out['modes']['f']['metrics']['citation_coverage']['mean'] == 0
    assert out['decision'] == 'unknown'
    assert out['gates']['quality_review']['passed'] is None


def test_family_equal_bootstrap_and_context_attribution():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    questions = [question('q1', 'big'), question('q2', 'big'), question('q3', 'small')]
    rows = [record(mode, qid=q['question_id'], covered=mode == 'b4' or q['question_id'] == 'q3')
            for q in questions for mode in MODES]
    kwargs = dict(expected_questions=3, bootstrap_samples=100)
    out = analyze(rows, questions, **kwargs)
    assert out == analyze(rows, questions, **kwargs)
    assert out['comparisons']['b4-a0']['context_coverage']['difference'] == .5
    assert out['attribution']['b4-a0']['changed_context']['citation_contribution'] == pytest.approx(2/3)
    assert out['attribution']['b4-a0']['same_context']['citation_contribution'] == 0
    assert out['gates']['quality_review']['passed'] is None


def test_failed_generation_cannot_create_context_gate_improvement():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = [record(mode, success=mode == 'b4') for mode in MODES]
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['gates']['context_coverage']['passed'] is False
    assert out['comparisons']['b4-a0']['citation_coverage']['difference'] == 1
    assert out['comparisons']['b4-a0']['context_coverage']['difference'] == 0


def test_final_protocol_requires_40_unique_questions():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    with pytest.raises(ValueError, match='40'):
        analyze([], [question()])


def test_all_provider_failures_produce_unknown_experiment_decision():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = [dict(question_id='q1', plugin=mode, repeat=0, service_success=False,
                 error_code='MODEL_UNAVAILABLE', response=None, retrieval_snapshot=None,
                 call_journal=dict(calls=[dict(stage='generation', failure_kind='network_error')]))
            for mode in MODES]
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['decision'] == 'unknown'
    assert out['experiment_validity']['status'] == 'invalid_infrastructure'


def test_native_navigation_trace_is_not_silently_free_or_zero_calls():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = []
    for mode in MODES:
        item = record(mode)
        if mode != 'a0':
            item['retrieval_snapshot']['retrieval'] = {'navigation': {
                'calls': [{'usage': None, 'elapsed_seconds': 1.5}], 'elapsed_seconds': 1.5}}
        rows.append(item)
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['modes']['f']['navigation']['calls'] == 1
    assert out['modes']['f']['navigation']['latency_ms']['p95'] == 1500
    assert out['modes']['f']['navigation']['call_budget_ok'] is True
    assert out['modes']['f']['usage']['complete'] is False


def test_native_navigation_usage_is_counted_once_even_with_shared_journal():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    item = record('b4')
    usage = dict(input_tokens=90, output_tokens=10)
    item['retrieval_snapshot']['retrieval'] = {'navigation': {
        'calls': [{'usage': usage, 'elapsed_seconds': 1.5, 'failure': 'invalid_selection'}],
        'elapsed_seconds': 1.5}, 'fallback': True}
    item['response']['trace']['call_journal']['calls'] = [dict(stage='navigation', usage=usage)]
    out = analyze([item], [question()], expected_questions=1, bootstrap_samples=20)
    report = out['modes']['b4']
    assert report['usage']['observed_input_tokens'] == 90
    assert report['usage']['observed_output_tokens'] == 10
    assert report['usage']['chat_calls'] == 1
    assert report['navigation']['fallback_requests'] == 1
    assert report['navigation']['failure_requests'] == 1


def test_native_navigation_and_journal_disagree_remains_unknown():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    item = record('b4')
    item['retrieval_snapshot']['retrieval'] = {'navigation': {
        'calls': [{'usage': dict(input_tokens=90, output_tokens=10)}]}}
    item['response']['trace']['call_journal']['calls'] = [
        dict(stage='navigation', usage=dict(input_tokens=91, output_tokens=10))]
    out = analyze([item], [question()], expected_questions=1, bootstrap_samples=20)
    assert out['modes']['b4']['usage']['complete'] is False
    assert out['modes']['b4']['usage']['navigation_disagreements'] == 1


def test_missing_context_observation_prevents_drop_but_semantic_empty_can_drop():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = [record(mode) for mode in MODES]
    rows[0].pop('retrieval_snapshot')
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['decision'] == 'unknown'
    assert out['experiment_validity']['status'] == 'unknown_prerequisites'
    rows = [record(mode) for mode in MODES]
    for item in rows:
        item['answer_status'] = 'insufficient'
        item['response']['claims'] = []
        item['response']['status'] = 'insufficient'
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['decision'] == 'drop_online_tree'
    assert out['experiment_validity']['status'] == 'valid'


def test_full_provider_outage_after_retrieval_has_no_valid_delivery_comparison():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = [record(mode, success=False) for mode in MODES]
    for item in rows:
        item['error_code'] = 'MODEL_UNAVAILABLE'
        item['call_journal']['calls'] = [dict(stage='generation', failure_kind='network_error', usage={})]
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['decision'] == 'unknown'
    assert out['experiment_validity']['status'] == 'invalid_infrastructure'


def test_missing_paid_usage_prevents_decision_even_when_other_gates_fail():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    rows = [record(mode) for mode in MODES]
    rows[2]['response']['trace']['call_journal']['calls'] = [dict(stage='generation', usage={})]
    out = analyze(rows, [question()], expected_questions=1, bootstrap_samples=20)
    assert out['decision'] == 'unknown'
    assert out['experiment_validity']['status'] == 'unknown_prerequisites'


def test_declared_navigation_count_cannot_hide_native_budget_overrun():
    from knowpath_backend.rag_eval.final_tree_analysis import analyze
    item = record('b4')
    item['retrieval_snapshot']['retrieval']['navigation'] = {'calls': [
        dict(usage=dict(input_tokens=1, output_tokens=1)) for _ in range(3)]}
    out = analyze([item], [question()], expected_questions=1, bootstrap_samples=20)
    assert out['modes']['b4']['navigation']['calls'] == 3
    assert out['modes']['b4']['navigation']['call_budget_ok'] is False


def test_runner_costs_actual_retrieval_navigation(harness, tmp_path, monkeypatch):
    factory, resolver = harness
    original = runner.verify_freeze
    def priced(path):
        frozen, questions, split = original(path)
        frozen['config']['pricing'] = dict(currency='CNY', date='2026-09-24', price_table={
            'chat': dict(input_per_million=1, output_per_million=1)})
        return frozen, questions, split
    monkeypatch.setattr(runner, 'verify_freeze', priced)
    class Priced(factory):
        def answer(self, *args, **kwargs):
            result = super().answer(*args, **kwargs)
            result['trace'].update(generation_calls=1, usage=[dict(model='chat', input_tokens=100, output_tokens=10)])
            result['trace']['retrieval'] = {'fallback': True, 'navigation': {'calls': [
                dict(usage=dict(model='chat', input_tokens=90, output_tokens=10), failure='invalid_selection')]}}
            return result
    rows = runner.run('freeze', tmp_path / 'costs.jsonl', pipeline_factory=Priced, scope_resolver=resolver)
    assert rows[0]['cost'] == pytest.approx(.00021)
