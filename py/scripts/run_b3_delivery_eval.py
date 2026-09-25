"""Isolated 40 x 3 x 3 delivery repair evaluation, without retries or resume."""
from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

PY = Path(__file__).resolve().parents[1]
WORK = PY.parent
MAIN = WORK.parents[1] if WORK.parent.name == '.worktrees' else WORK
BASE = PY / '.rag-evaluation'
SOURCE = BASE / 'tree-b3-repair-40-v2'
ARCHIVE = BASE / 'tree-b3-delivery-repair-baseline-20260923'
TARGET = BASE / 'tree-b3-delivery-repair-40-v1'
PREFIX = 'knowpath_rag_b3_repair_40_v2'
MODES = ('a', 'b2_r1', 'b3_unit')
DATASET_HASH = '230bf4a271ba2bc2fc2d774357caf2bc2e21b46ae5eccf7c9e190b05a1d9eeb2'
ANALYSIS_PLAN = dict(version='delivery-paired-family-v1', seed=20260923, bootstrap_samples=10000,
    confidence=.95, repeats=3, primary='b3_unit-a', secondary='b2_r1-a', exploratory=True,
    failures='zero_delivery_unknown_unobserved_retrieval', weighting='equal_family',
    repeat_aggregation='question_mean', human_correctness='unknown_pending_external_review')
sys.path.insert(0, str(PY))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def verify_archive_mapping(mapping):
    for entry in mapping.values():
        if file_hash(entry['archive']) != entry['sha256']:
            raise ValueError('ARCHIVE_HASH_MISMATCH')


def source_hashes():
    paths=set((PY/'knowpath_backend/learning').rglob('*.py'))
    paths.update((PY/'knowpath_backend/rag_eval').glob('*.py'))
    paths.update((PY/'knowpath_backend/test').glob('test_rag*.py'))
    paths.update([Path(__file__),PY/'scripts/run_b3_repair_eval.py',PY/'scripts/replay_b3_delivery_repair.py',
                  PY/'scripts/report_b3_delivery_eval.py',
                  PY/'pyproject.toml',PY/'uv.lock'])
    return {str(p):file_hash(p) for p in sorted(paths) if p.is_file()}


def verify_gate_evidence(evidence, current):
    if evidence.get('source_hashes') != current:
        raise ValueError('STALE_GATE_EVIDENCE')


def verify_schedule(schedule, questions, config):
    if (len(questions)!=40 or len({q['question_id'] for q in questions})!=40
            or config.get('modes')!=list(MODES) or config.get('repeats')!=3
            or config.get('retries')!=0 or config.get('order_schedule')!='alternating'
            or schedule!=planned_schedule(questions)):
        raise ValueError('INVALID_SCHEDULE')


def planned_schedule(questions):
    result = []
    for i, question in enumerate(questions):
        for repeat in range(3):
            offset = (i * 3 + repeat) % len(MODES)
            order = MODES[offset:] + MODES[:offset]
            result.extend(dict(question_id=question['question_id'], plugin=mode,
                               repeat=repeat, order=position) for position, mode in enumerate(order))
    return result


def configure():
    from dotenv import load_dotenv
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.rag_eval.cli import runtime_configuration
    load_dotenv(MAIN / 'py/.env', override=True)
    os.environ.update(DATABASE_URL='sqlite:///' + (TARGET / 'evaluation.db').as_posix(),
        RAG_COLLECTION_PREFIX=PREFIX, QDRANT_URL='http://127.0.0.1:6333',
        RAG_MODEL_INPUT_TOKENS='14000', RAG_MODEL_OUTPUT_TOKENS='2000',
        LEARNING_CONTEXT_BUDGET_TOKENS='16000', RAG_DEADLINE_SECONDS='240',
        LEARNING_CHAT_TIMEOUT_SECONDS='120', RAG_MAX_DRAFT_BYTES='4000',
        RAG_RESPONSE_FORMAT='json_object', RAG_REVISION_GENERATION_SECONDS='10',
        RAG_REVISION_VERIFICATION_SECONDS='25')
    settings = LearningSettings.from_env()
    actual = runtime_configuration(settings)
    old = read(ARCHIVE / 'py/.rag-evaluation/tree-b3-repair-40-v2/development.json')
    expected_budget = dict(old['budgets'], deadline_seconds=240.0, chat_timeout_seconds=120.0)
    if actual['models'] != old['models'] or actual['prompts'] != old['prompts']:
        raise ValueError('MODEL_OR_PROTOCOL_CHANGED')
    if actual['budgets'] != expected_budget or actual['environment'] != old['environment']:
        raise ValueError('UNEXPECTED_EXPERIMENT_CONFIGURATION')
    return settings, actual


def legacy_readonly_helpers():
    """Reuse verified index/scope readers; never invoke their mutation stages."""
    path = PY / 'scripts/run_b3_repair_eval.py'
    spec = importlib.util.spec_from_file_location('delivery_existing_index_readers', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TARGET = TARGET
    module.PREFIX = PREFIX
    module.configure = configure
    return module


def prepare():
    verify_archive_mapping(read(ARCHIVE / 'path-map.json'))
    archived_source=ARCHIVE/'py/.rag-evaluation/tree-b3-repair-40-v2'
    if file_hash(archived_source / 'dataset.jsonl') != DATASET_HASH:
        raise ValueError('DATASET_CHANGED')
    TARGET.mkdir(exist_ok=False)
    for name in ('dataset.jsonl', 'split.json', 'rubric.md', 'index-snapshot.json',
                 'unit-manifest.json', 'structure-audit.json'):
        shutil.copyfile(archived_source / name, TARGET / name)
    shutil.copytree(archived_source / 'sources', TARGET / 'sources')
    with closing(sqlite3.connect((archived_source / 'evaluation.db').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(TARGET / 'evaluation.db')) as target:
            source.backup(target)
    _, actual = configure()
    config = read(archived_source / 'development.json')
    config.update(actual)
    config.update(repeats=3, modes=list(MODES), retries=0, human_review_status='pending')
    config['delivery_experiment'] = dict(baseline_archive=str(ARCHIVE),
        original_dataset_sha256=DATASET_HASH, analysis='analysis-plan.json',
        shared_index_read_only=True, historical_comparison='background_only_mixed_retries_and_timeouts',
        adoption_gates=dict(context_coverage_gain=.03, negative_control_regression=0, p95_ratio=1.2))
    write(TARGET / 'development.json', config)
    questions = [json.loads(line) for line in (TARGET / 'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    if len(questions) != 40:
        raise ValueError('EXPECTED_40_QUESTIONS')
    write(TARGET / 'schedule.json', planned_schedule(questions))
    write(TARGET / 'analysis-plan.json', ANALYSIS_PLAN)
    preflight()


def preflight():
    configure()
    legacy_readonly_helpers().preflight()
    schedule = read(TARGET / 'schedule.json')
    questions=[json.loads(line) for line in (TARGET/'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    verify_schedule(schedule,questions,read(TARGET/'development.json'))
    split=read(TARGET/'split.json')
    if (set(split['dev']['question_ids'])!={q['question_id'] for q in questions}
            or read(TARGET/'analysis-plan.json')!=ANALYSIS_PLAN):
        raise ValueError('ANALYSIS_OR_PARTITION_CHANGED')
    archived_source=ARCHIVE/'py/.rag-evaluation/tree-b3-repair-40-v2'
    for name in ('dataset.jsonl','split.json','rubric.md','unit-manifest.json','index-snapshot.json','structure-audit.json'):
        if file_hash(TARGET/name)!=file_hash(archived_source/name):
            raise ValueError('ORIGINAL_ARTIFACT_CHANGED')


def check_stop_condition(record):
    trace=(record.get('response') or {}).get('trace',{})
    journal=trace.get('call_journal',record.get('call_journal')) or {}
    failures={c.get('failure_kind') for c in journal.get('calls',[])}
    failures.add(journal.get('failure_kind'))
    failures.add((record.get('error_details') or {}).get('failure_kind'))
    if 'authentication' in failures:
        raise RuntimeError('EXPERIMENT_STOP_AUTHENTICATION')
    if record.get('error_code') in {'RAG_COST_BUDGET_EXCEEDED','RAG_SPEND_CONFIG_INVALID'}:
        raise RuntimeError('EXPERIMENT_STOP_BUDGET')


def freeze_run():
    from knowpath_backend.rag_eval.dataset import freeze, digest, verify_freeze
    preflight()
    validation = read(TARGET / 'validation.json')
    replay = read(TARGET / 'offline-replay.json')
    if validation.get('passed') is not True or replay.get('gate_passed') is not True:
        raise ValueError('OFFLINE_GATE_NOT_PASSED')
    current=source_hashes()
    for evidence in (validation,replay,read(TARGET/'review-attestation.json')):
        verify_gate_evidence(evidence,current)
    if read(TARGET/'review-attestation.json').get('passed') is not True:
        raise ValueError('REVIEW_NOT_PASSED')
    for name in ('spec-review.md','quality-review.md'):
        if not (TARGET / name).is_file():
            raise ValueError('REVIEW_MISSING')
    frozen = freeze(TARGET / 'development.json', TARGET / 'freeze-base.json')
    sidecars = ['schedule.json','analysis-plan.json','index-snapshot.json','unit-manifest.json',
                'structure-audit.json','validation.json','offline-replay.json','review-attestation.json','spec-review.md','quality-review.md']
    paths = [TARGET/name for name in sidecars] + [Path(__file__),PY/'scripts/run_b3_repair_eval.py',
                PY/'scripts/replay_b3_delivery_repair.py',PY/'scripts/report_b3_delivery_eval.py']
    for path in paths:
        frozen['file_hashes'][str(path)] = file_hash(path)
    frozen['freeze_id'] = digest({k:v for k,v in frozen.items() if k != 'freeze_id'})
    write(TARGET / 'freeze.json', frozen)
    verify_freeze(TARGET / 'freeze.json')
    with zipfile.ZipFile(TARGET/'frozen-code.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for name in frozen['file_hashes']:
            path=Path(name)
            if path.is_relative_to(PY) and not path.is_relative_to(TARGET):
                archive.write(path,path.relative_to(PY).as_posix())
    print(json.dumps(dict(freeze_id=frozen['freeze_id'],scheduled_requests=360)),flush=True)


def execute():
    from knowpath_backend.rag_eval.cli import configured_runtime
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.runner import run
    configure()
    frozen, questions, split = verify_freeze(TARGET/'freeze.json')
    if len(questions)!=40 or len(split['dev']['question_ids'])!=40 or frozen['config']['repeats']!=3:
        raise ValueError('SCHEDULE_CHANGED')
    preflight()
    factory,resolver,engine=configured_runtime(frozen)
    completed=0
    def before_request():
        verify_freeze(TARGET/'freeze.json')
    def after_record(record):
        nonlocal completed
        completed+=1
        print(json.dumps(dict(completed=completed,total=360,question_id=record['question_id'],
            mode=record['plugin'],repeat=record['repeat'],service_success=record['service_success'],
            status=record['answer_status'])),flush=True)
        check_stop_condition(record)
    try:
        records=run(TARGET/'freeze.json',TARGET/'dev.jsonl',partition='dev',
                    pipeline_factory=factory,scope_resolver=resolver,
                    before_request=before_request,after_record=after_record)
    finally:
        engine.dispose()
    verify_freeze(TARGET/'freeze.json')
    preflight()
    print(json.dumps(dict(attempted=len(records),service_successes=sum(r['service_success'] for r in records))),flush=True)
    score()


def score():
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.scoring import report
    from knowpath_backend.rag_eval.delivery_analysis import analyze
    frozen,questions,_=verify_freeze(TARGET/'freeze.json')
    records=[json.loads(line) for line in (TARGET/'dev.jsonl').read_text(encoding='utf-8').splitlines()]
    write(TARGET/'report.json',report(TARGET/'freeze.json',TARGET/'dev.jsonl'))
    plan=read(TARGET/'analysis-plan.json')
    if plan!=ANALYSIS_PLAN:
        raise ValueError('ANALYSIS_PLAN_CHANGED')
    result=analyze(records,questions,repeats=plan['repeats'],modes=MODES,
                   seed=plan['seed'],bootstrap_samples=plan['bootstrap_samples'])
    write(TARGET/'delivery-analysis.json',result)
    from report_b3_delivery_eval import build
    build()
    print(json.dumps(dict(scheduled=360,actual=len(records),analysis=str(TARGET/'delivery-analysis.json'))),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('prepare','preflight','freeze','run','report'))
    args=parser.parse_args()
    try:
        {'prepare':prepare,'preflight':preflight,'freeze':freeze_run,'run':execute,'report':score}[args.stage]()
    except Exception as error:
        print(json.dumps(dict(stage=args.stage,error_type=type(error).__name__,status='experiment_stopped')),file=sys.stderr,flush=True)
        raise SystemExit(1) from None
