"""Fresh user-authorized 40 x 3 x 1 batch; original frozen run stays intact."""
from contextlib import closing
from pathlib import Path
import argparse
import json
import shutil
import sqlite3
import sys

import run_b3_delivery_eval as base

ORIGINAL = base.TARGET
TARGET = base.BASE / 'tree-b3-delivery-repair-40-single-v1'
base.TARGET = TARGET
PLAN = dict(base.ANALYSIS_PLAN, repeats=1)


def schedule(questions):
    result = []
    for i, q in enumerate(questions):
        offset = i % len(base.MODES)
        order = base.MODES[offset:] + base.MODES[:offset]
        result.extend(dict(question_id=q['question_id'], plugin=m, repeat=0, order=j)
                      for j, m in enumerate(order))
    return result


def preflight():
    from knowpath_backend.rag_eval.dataset import verify_freeze
    verify_freeze(ORIGINAL / 'freeze.json')
    base.configure()
    base.legacy_readonly_helpers().preflight()
    questions = [json.loads(s) for s in (TARGET/'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    config = base.read(TARGET/'development.json')
    if (len(questions) != 40 or len({q['question_id'] for q in questions}) != 40
            or config['repeats'] != 1 or config['retries'] != 0
            or config['modes'] != list(base.MODES) or config['order_schedule'] != 'alternating'
            or base.read(TARGET/'schedule.json') != schedule(questions)
            or base.read(TARGET/'analysis-plan.json') != PLAN):
        raise ValueError('SINGLE_BATCH_CONFIGURATION_CHANGED')
    for name in ('dataset.jsonl','split.json','rubric.md','unit-manifest.json','index-snapshot.json','structure-audit.json'):
        if base.file_hash(TARGET/name) != base.file_hash(ORIGINAL/name):
            raise ValueError('ORIGINAL_ARTIFACT_CHANGED')
    return questions


def prepare():
    from knowpath_backend.rag_eval.dataset import freeze, digest, verify_freeze
    verify_freeze(ORIGINAL/'freeze.json')
    for name in ('validation.json','offline-replay.json','review-attestation.json'):
        evidence = base.read(ORIGINAL/name)
        base.verify_gate_evidence(evidence, base.source_hashes())
        if evidence.get('passed', evidence.get('gate_passed')) is not True:
            raise ValueError('ORIGINAL_GATE_NOT_PASSED')
    TARGET.mkdir(exist_ok=False)
    for name in ('dataset.jsonl','split.json','rubric.md','index-snapshot.json','unit-manifest.json','structure-audit.json'):
        shutil.copyfile(ORIGINAL/name, TARGET/name)
    shutil.copytree(ORIGINAL/'sources', TARGET/'sources')
    with closing(sqlite3.connect((ORIGINAL/'evaluation.db').as_uri()+'?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(TARGET/'evaluation.db')) as target:
            source.backup(target)
    _, actual = base.configure()
    config = base.read(ORIGINAL/'development.json')
    config.update(actual)
    config['repeats'] = 1
    config['delivery_experiment'].update(parent_freeze=base.read(ORIGINAL/'freeze.json')['freeze_id'],
        user_requested_single_run=True, previous_records_excluded=True)
    base.write(TARGET/'development.json', config)
    questions = [json.loads(s) for s in (TARGET/'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    base.write(TARGET/'schedule.json', schedule(questions))
    base.write(TARGET/'analysis-plan.json', PLAN)
    preflight()
    frozen = freeze(TARGET/'development.json', TARGET/'freeze-base.json')
    frozen['file_hashes'].update(base.read(ORIGINAL/'freeze.json')['file_hashes'])
    paths = [Path(__file__), base.PY/'scripts/report_b3_delivery_single_eval.py']
    paths += [TARGET/name for name in ('schedule.json','analysis-plan.json','index-snapshot.json','unit-manifest.json','structure-audit.json')]
    for path in paths:
        frozen['file_hashes'][str(path)] = base.file_hash(path)
    frozen['freeze_id'] = digest({k:v for k,v in frozen.items() if k != 'freeze_id'})
    base.write(TARGET/'freeze.json', frozen)
    verify_freeze(TARGET/'freeze.json')
    print(json.dumps(dict(stage='prepared', total=120, freeze_id=frozen['freeze_id'])), flush=True)


def score():
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.scoring import report
    from knowpath_backend.rag_eval.delivery_analysis import analyze
    import report_b3_delivery_single_eval as reporting
    _, questions, _ = verify_freeze(TARGET/'freeze.json')
    records = [json.loads(s) for s in (TARGET/'dev.jsonl').read_text(encoding='utf-8').splitlines()]
    expected = schedule(questions)
    if len(records) > 120 or any({k:r[k] for k in ('question_id','plugin','repeat','order')} != expected[i]
                                 for i,r in enumerate(records)):
        raise ValueError('RECORD_SCHEDULE_MISMATCH')
    base.write(TARGET/'report.json', report(TARGET/'freeze.json', TARGET/'dev.jsonl'))
    base.write(TARGET/'delivery-analysis.json', analyze(records, questions, repeats=1, modes=base.MODES,
        seed=PLAN['seed'], bootstrap_samples=PLAN['bootstrap_samples']))
    reporting.TARGET = TARGET
    reporting.build()
    print(json.dumps(dict(stage='reported', actual=len(records), scheduled=120)), flush=True)


def execute():
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.cli import configured_runtime
    from knowpath_backend.rag_eval.runner import run
    preflight()
    frozen, _, _ = verify_freeze(TARGET/'freeze.json')
    factory, resolver, engine = configured_runtime(frozen)
    completed = 0
    def before():
        verify_freeze(TARGET/'freeze.json')
    def after(record):
        nonlocal completed
        completed += 1
        print(json.dumps(dict(completed=completed, total=120, question_id=record['question_id'],
            mode=record['plugin'], service_success=record['service_success'], status=record['answer_status'])), flush=True)
        base.check_stop_condition(record)
    try:
        run(TARGET/'freeze.json', TARGET/'dev.jsonl', partition='dev', pipeline_factory=factory,
            scope_resolver=resolver, before_request=before, after_record=after)
    finally:
        engine.dispose()
    preflight()
    score()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare','preflight','run','report'))
    args = parser.parse_args()
    try:
        {'prepare':prepare, 'preflight':preflight, 'run':execute, 'report':score}[args.stage]()
    except Exception as error:
        print(json.dumps(dict(stage=args.stage, error_type=type(error).__name__, status='experiment_stopped')), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
