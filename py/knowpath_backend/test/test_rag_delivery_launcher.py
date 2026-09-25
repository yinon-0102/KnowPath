import importlib.util
from pathlib import Path

import pytest


def launcher():
    path = Path(__file__).resolve().parents[2] / 'scripts/run_b3_delivery_eval.py'
    spec = importlib.util.spec_from_file_location('delivery_launcher', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schedule_exactly_360_unique_balanced_keys():
    module = launcher()
    questions = [dict(question_id=f'q{i}') for i in range(40)]
    rows = module.planned_schedule(questions)
    assert len(rows) == 360
    assert len({(r['question_id'], r['plugin'], r['repeat']) for r in rows}) == 360
    for q in questions:
        for mode in ('a', 'b2_r1', 'b3_unit'):
            assert sorted(r['order'] for r in rows if r['question_id']==q['question_id'] and r['plugin']==mode) == [0,1,2]


def test_archive_verification_checks_copies_not_mutated_original(tmp_path):
    module = launcher()
    original = tmp_path / 'live.py'
    archived = tmp_path / 'baseline.py'
    original.write_text('after')
    archived.write_text('before')
    mapping = {str(original): dict(archive=str(archived), sha256=module.file_hash(archived))}
    module.verify_archive_mapping(mapping)
    archived.write_text('corrupted')
    with pytest.raises(ValueError, match='ARCHIVE_HASH_MISMATCH'):
        module.verify_archive_mapping(mapping)


def test_exclusive_json_does_not_overwrite(tmp_path):
    module = launcher()
    path = tmp_path / 'results.json'
    module.write(path, {'first': True})
    with pytest.raises(FileExistsError):
        module.write(path, {'replacement': True})
    assert module.read(path) == {'first': True}


def test_global_stop_conditions_do_not_hide_failed_row():
    module = launcher()
    with pytest.raises(RuntimeError, match='AUTHENTICATION'):
        module.check_stop_condition(dict(error_code='MODEL_ERROR',
            call_journal=dict(calls=[dict(failure_kind='authentication')],status='failed')))
    with pytest.raises(RuntimeError, match='BUDGET'):
        module.check_stop_condition(dict(error_code='RAG_COST_BUDGET_EXCEEDED'))
    module.check_stop_condition(dict(error_code='RAG_DEADLINE_EXCEEDED'))
    module.check_stop_condition(dict(error_code='VERIFICATION_UNAVAILABLE'))


def test_runner_callbacks_preserve_append_before_stop(tmp_path, monkeypatch):
    from knowpath_backend.rag_eval import runner
    from knowpath_backend.rag_eval.dataset import digest
    config = dict(repeats=1,modes=['a','b2_r1','b3_unit'],pricing={},runtime_bindings={})
    binding=dict(space_id='s',scope_snapshot_id='scope',expected_scope_version=1,expected_bindings=[],
        expected_manifest_ids=['m'],expected_retrieval_versions=['rv'],manifest_configuration_hashes={'m':digest({'x':1})})
    config['runtime_bindings']['q']=binding
    frozen=dict(config=config,freeze_id='f')
    questions=[dict(question_id='q',family_id='family',question='question',scope_snapshot_id='scope')]
    monkeypatch.setattr(runner,'verify_freeze',lambda path:(frozen,questions,{}))
    monkeypatch.setattr(runner,'authorize_partition',lambda *args:questions)
    actual=dict(space_id='s',scope_snapshot_id='scope',scope_version=1,bindings=[],
        manifests=[dict(manifest_id='m',retrieval_version_id='rv',configuration={'x':1})])
    events=[]
    class Pipeline:
        def answer(self,*args,**kwargs):
            raise RuntimeError('provider response must not leak')
    def before():
        events.append('before')
    output=tmp_path/'runs.jsonl'
    def after(row):
        assert output.read_text(encoding='utf-8').count('\n') == 1
        assert row['service_success'] is False
        events.append('after')
        raise RuntimeError('stop')
    with pytest.raises(RuntimeError,match='stop'):
        runner.run('unused',output,pipeline_factory=lambda mode:Pipeline(),scope_resolver=lambda space:actual,
            before_request=before,after_record=after)
    assert events == ['before','after']
    assert 'provider response' not in output.read_text(encoding='utf-8')


def test_gate_evidence_must_match_current_sources(tmp_path):
    module=launcher()
    path=tmp_path/'module.py'
    path.write_text('old')
    hashes={str(path):module.file_hash(path)}
    module.verify_gate_evidence({'source_hashes':hashes},hashes)
    path.write_text('changed')
    with pytest.raises(ValueError,match='STALE_GATE_EVIDENCE'):
        module.verify_gate_evidence({'source_hashes':hashes},{str(path):module.file_hash(path)})
    with pytest.raises(ValueError,match='STALE_GATE_EVIDENCE'):
        module.verify_gate_evidence({},hashes)


def test_schedule_requires_exact_question_mode_repeat_order():
    module=launcher()
    questions=[dict(question_id=f'q{i}') for i in range(40)]
    rows=module.planned_schedule(questions)
    config=dict(modes=['a','b2_r1','b3_unit'],repeats=3,retries=0,order_schedule='alternating')
    module.verify_schedule(rows,questions,config)
    rows[0]['question_id']='invented'
    with pytest.raises(ValueError,match='INVALID_SCHEDULE'):
        module.verify_schedule(rows,questions,config)
