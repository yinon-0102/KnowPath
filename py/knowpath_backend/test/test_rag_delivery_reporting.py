import importlib.util
from pathlib import Path
import pytest


def reporting():
    path=Path(__file__).resolve().parents[2]/'scripts/report_b3_delivery_eval.py'
    spec=importlib.util.spec_from_file_location('delivery_reporting',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unit_recall_requires_complete_gold_unit_not_single_member():
    module=reporting()
    span=dict(material_version_id='m',artifact_hash='h',page=None,block='b',start=0,end=10)
    unit=dict(unit_id='u',leaf_ids=['a','b'],source_spans=[span])
    assert module.full_unit_recall(['a'],[span],[unit],10)==0
    assert module.full_unit_recall(['a','b'],[span],[unit],10)==1


def test_blind_review_does_not_expose_mode_or_raw_trace():
    module=reporting()
    records=[dict(question_id='q',plugin='b3_unit',repeat=0,service_success=True,
        response=dict(text='答案',claims=[dict(text='结论')],sources=[],citations=[],trace={'secret':'trace'}))]
    package,mapping=module.blind_review(records,[dict(question_id='q',question='题目')])
    assert len(package)==1 and len(mapping)==1
    assert 'plugin' not in package[0] and 'trace' not in str(package)
    assert mapping[package[0]['review_id']]['plugin']=='b3_unit'
    assert package[0]['human_correctness'] is None


def test_report_rejects_stale_analysis():
    module=reporting()
    with pytest.raises(ValueError,match='ANALYSIS_RESULTS_CHANGED'):
        module.verify_analysis_records({'input_sha256':'old'},[])
