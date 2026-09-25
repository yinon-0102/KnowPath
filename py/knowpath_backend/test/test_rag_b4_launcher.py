import importlib.util
from pathlib import Path


def launcher():
    path = Path(__file__).resolve().parents[2] / 'scripts/run_b4_final_eval.py'
    spec = importlib.util.spec_from_file_location('b4_launcher_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_final_schedule_is_exactly_one_pass_of_120_keys():
    module = launcher()
    questions = [{'question_id': str(i)} for i in range(40)]
    schedule = module.schedule(questions)
    assert len(schedule) ==120
    assert len({(r['question_id'],r['plugin']) for r in schedule})==120
    assert {r['repeat'] for r in schedule}=={0}
    assert [r['plugin'] for r in schedule[:6]] == ['a0','f','b4','f','b4','a0']


def test_final_schedule_rejects_120_questions_or_duplicates():
    import pytest
    module = launcher()
    for questions in ([{'question_id':str(i)} for i in range(120)], [{'question_id':'same'}]*40):
        with pytest.raises(ValueError):
            module.schedule(questions)


def test_index_accounting_repairs_missing_local_artifact_without_provider(tmp_path,monkeypatch):
    from types import SimpleNamespace
    module=launcher()
    monkeypatch.setattr(module,'TARGET',tmp_path)
    index=SimpleNamespace(index_id='fixed',usage=[{'kind':'embedding','usage':{'input_tokens':3}}],
                          plan=SimpleNamespace(report={'summary_calls':2}))
    module.ensure_index_accounting(index)
    result=module.read(tmp_path/'index-build.json')
    assert result['index_id']=='fixed' and result['usage']==index.usage
    module.ensure_index_accounting(index)


def test_index_writer_lock_rejects_second_writer(tmp_path):
    import pytest
    module=launcher()
    with module.exclusive_lock(tmp_path/'index.lock'):
        with pytest.raises(FileExistsError):
            with module.exclusive_lock(tmp_path/'index.lock'):
                raise AssertionError('entered concurrent writer')


def test_index_accounting_keeps_earlier_unknown_paid_attempt(tmp_path,monkeypatch):
    from types import SimpleNamespace
    module=launcher()
    monkeypatch.setattr(module,'TARGET',tmp_path)
    module.write(tmp_path/'initial-index-stop.json',{'unresolved_summary_calls':[
        {'call_id':'prior','billing_status':'unknown','original_error_code':'MODEL_UNAVAILABLE'}]})
    index=SimpleNamespace(index_id='fixed',usage=[],plan=SimpleNamespace(report={'summary_calls':220}))
    module.ensure_index_accounting(index)
    result=module.read(tmp_path/'index-build.json')
    assert result['prior_unresolved_summary_attempts'][0]['billing_status']=='unknown'
    assert result['accounting_complete'] is False
