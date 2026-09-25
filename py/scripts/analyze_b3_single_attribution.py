"""Offline comparison of reconstructed initial requests; no model or credential access."""
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from replay_b3_delivery_repair import audit, digest

sys.dont_write_bytecode = True
sys.addaudithook(audit)
PY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PY))
from knowpath_backend.learning.rag.capacity import AnswerCapacity
from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
from knowpath_backend.learning.rag.token_budget import ConservativeByteProfile
from knowpath_backend.learning.rag.verification import AnswerVerifier
from knowpath_backend.rag_eval.dataset import verify_freeze

TARGET = PY / '.rag-evaluation/tree-b3-delivery-repair-40-single-v1'


def execute():
    frozen, questions, _ = verify_freeze(TARGET/'freeze.json')
    config = frozen['config']
    budgets, protocol = config['budgets'], config['prompts']['protocol']
    records = [json.loads(s) for s in (TARGET/'dev.jsonl').read_text(encoding='utf-8').splitlines()]
    analysis = json.loads((TARGET/'delivery-analysis.json').read_text(encoding='utf-8'))
    if analysis['input_sha256'] != digest(records) or len(records) != 120:
        raise ValueError('COMPLETE_BOUND_RESULTS_REQUIRED')
    model = object.__new__(BudgetedJsonModel)
    model.settings = SimpleNamespace(chat_model=config['models']['chat_model'], chat_provider=config['models']['chat_provider'],
                                     context_budget_tokens=budgets['model_context_tokens'])
    model.max_input_tokens = budgets['model_input_tokens']
    model.max_output_tokens = budgets['model_output_tokens']
    model.max_draft_bytes = protocol['max_draft_bytes']
    model.response_format = protocol['response_format']
    cp = protocol['counting_profile']
    model.counting_profile = ConservativeByteProfile(cp['provider'], cp['model'], cp['per_message_overhead'], cp['request_overhead'])
    def forbidden(*args, **kwargs):
        raise AssertionError('NO_MODEL_CALL_ALLOWED')
    model.generate_json = forbidden
    verifier = AnswerVerifier(model, model)
    capacity = AnswerCapacity(verifier, generation_seconds=10, verification_seconds=25)
    by = {(r['question_id'], r['plugin']): r for r in records}
    rows = []
    for q in questions:
        row = dict(question_id=q['question_id'], initial_request_hashes={}, modes={})
        for mode in ('a','b2_r1','b3_unit'):
            record = by[q['question_id'],mode]
            response = record.get('response') or {}
            trace = response.get('trace', {})
            query = trace.get('query', {}).get('query')
            sources = response.get('sources')
            request_hash = None
            if record['service_success'] and query is not None and sources:
                request_hash = digest(capacity._requests(*verifier.initial_messages(query, sources))[0].body)
            row['initial_request_hashes'][mode] = request_hash
            metrics = analysis['per_question'][mode][q['question_id']]
            row['modes'][mode] = dict(status=record['answer_status'], delivery=trace.get('delivery'),
                context_ids=trace.get('context_ids'), context_coverage=metrics['context_coverage'],
                citation_coverage=metrics['citation_coverage'], anchor_repacking=trace.get('anchor_repacking'),
                retrieval=trace.get('retrieval'), latency_ms=record.get('latency_ms'))
        a,b = (row['initial_request_hashes'][m] for m in ('a','b3_unit'))
        row['a_b3_initial_input'] = ('identical' if a == b else 'different') if a and b else 'unknown'
        for metric in ('context_coverage','citation_coverage'):
            a,b = (row['modes'][m][metric] for m in ('a','b3_unit'))
            row[metric+'_delta'] = b-a if a is not None and b is not None else None
        rows.append(row)
    groups = {}
    for label in ('identical','different','unknown'):
        selected = [r for r in rows if r['a_b3_initial_input'] == label]
        groups[label] = dict(questions=len(selected), question_ids=[r['question_id'] for r in selected])
        for metric in ('context_coverage','citation_coverage'):
            values = [r[metric+'_delta'] for r in selected]
            groups[label][metric+'_overall_contribution'] = sum(values)/40 if all(v is not None for v in values) else None
    result = dict(kind='offline_reconstructed_initial_request_comparison', records=120,
        input_sha256=analysis['input_sha256'], groups=groups, rows=rows,
        caution='Identical initial input does not imply identical later model responses; this is attribution evidence, not a causal experiment.')
    with (TARGET/'input-attribution.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(groups, ensure_ascii=False))


if __name__ == '__main__':
    execute()
