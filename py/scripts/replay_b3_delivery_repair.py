"""Credential-free offline replay against archived baseline and repaired code.

Run --implementation baseline first, then --implementation current. Each uses
an isolated interpreter import root; no subprocess, network or dotenv reads.
"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

PY=Path(__file__).resolve().parents[1]
BASE=PY/'.rag-evaluation'
ARCHIVE=BASE/'tree-b3-delivery-repair-baseline-20260923'
SOURCE=ARCHIVE/'py/.rag-evaluation/tree-b3-repair-40-v2'
TARGET=BASE/'tree-b3-delivery-repair-40-v1'


def audit(event,args):
    if event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system'}:
        raise AssertionError('OFFLINE_NETWORK_OR_PROCESS_FORBIDDEN')
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        if any(p.lower().startswith('.env') for p in Path(os.fsdecode(args[0])).parts):
            raise AssertionError('OFFLINE_CREDENTIAL_READ_FORBIDDEN')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def execute(implementation):
    sys.dont_write_bytecode=True
    sys.addaudithook(audit)
    root=ARCHIVE/'py' if implementation=='baseline' else PY
    sys.path.insert(0,str(root))
    from knowpath_backend.learning.rag.capacity import AnswerCapacity
    from knowpath_backend.learning.rag.context import explicit_anchor_rows,preserve_baseline_with_anchors
    from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
    from knowpath_backend.learning.rag.token_budget import ConservativeByteProfile
    from knowpath_backend.learning.rag.verification import AnswerVerifier
    from knowpath_backend.rag_eval.scoring import _union_covered_ids
    mapping=read(ARCHIVE/'path-map.json')
    for entry in mapping.values():
        if sha256(Path(entry['archive']).read_bytes()).hexdigest()!=entry['sha256']:
            raise ValueError('ARCHIVE_CHANGED')
    config=read(SOURCE/'freeze.json')['config']
    budgets,protocol=config['budgets'],config['prompts']['protocol']
    runs=[json.loads(line) for line in (SOURCE/'retry-two-extended-v1/merged.jsonl').read_text(encoding='utf-8').splitlines()]
    gold={q['question_id']:q for q in map(json.loads,(SOURCE/'dataset.jsonl').read_text(encoding='utf-8').splitlines())}
    unit_by_leaf={cid:u for u in read(SOURCE/'unit-manifest.json')['units'] for cid in u['leaf_ids']}
    versions={rv for run in runs for rv in run['response']['trace']['retrieval_versions']}
    conn=sqlite3.connect((SOURCE/'evaluation.db').as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        chunks={cid:json.loads(payload) for rv,cid,payload in conn.execute(
            'SELECT retrieval_version_id,chunk_id,payload FROM rag_chunks') if rv in versions}
    finally:
        conn.close()
    def enrich(cid):
        row=deepcopy(chunks[cid]); unit=unit_by_leaf[cid]
        row.update(evidence_group=row.get('continuation_of') or cid,
            requires=sorted(set(row.get('requires',[]))|(set(unit['leaf_ids'])-{cid})),
            tree_version_id=unit['tree_version_id'])
        return row
    def forbidden(*args,**kwargs):
        raise AssertionError('OFFLINE_MODEL_CALL_FORBIDDEN')
    model=object.__new__(BudgetedJsonModel)
    model.settings=SimpleNamespace(chat_model=config['models']['chat_model'],chat_provider=config['models']['chat_provider'],
                                   context_budget_tokens=budgets['model_context_tokens'])
    model.max_input_tokens=budgets['model_input_tokens'];model.max_output_tokens=budgets['model_output_tokens']
    model.max_draft_bytes=protocol['max_draft_bytes'];model.response_format=protocol['response_format']
    cp=protocol['counting_profile']
    model.counting_profile=ConservativeByteProfile(cp['provider'],cp['model'],cp['per_message_overhead'],cp['request_overhead'])
    model.generate_json=forbidden
    verifier=AnswerVerifier(model,model)
    capacity=AnswerCapacity(verifier,generation_seconds=10,verification_seconds=25)
    rows=[]
    for run in runs:
        trace=run['response']['trace'];query=trace['query']['query']
        ordered=[enrich(cid) for cid in trace['reranked_ids']]
        if implementation=='baseline':
            selected,ct=capacity.select_context(query,ordered,max_evidence_tokens=budgets['context_tokens'])
            missing=[r for r in explicit_anchor_rows(query,ordered) if r['chunk_id'] in set(ct['omitted_chunk_ids'])]
            if missing:
                other,other_ct=capacity.select_context(query,preserve_baseline_with_anchors(query,ordered,selected),
                    max_evidence_tokens=budgets['context_tokens'])
                if any(r['chunk_id'] in {s['chunk_id'] for s in other} for r in missing):
                    selected,ct=other,other_ct
            decision={'reason':'historical_baseline'}
            if [r['chunk_id'] for r in selected]!=trace['context_ids'] or ct!=trace['protocol_capacity']:
                raise ValueError('BASELINE_CONTEXT_OR_TRACE_MISMATCH')
            for stored,reconstructed in zip(run['response']['sources'],selected,strict=True):
                if any(stored[k]!=reconstructed[k] for k in ('source_text','source_spans','chunk_id','requires','evidence_group')):
                    raise ValueError('BASELINE_SOURCE_MISMATCH')
        else:
            from knowpath_backend.learning.rag.pipeline import select_context_with_anchors
            selected,ct,decision=select_context_with_anchors(capacity,query,ordered,max_evidence_tokens=budgets['context_tokens'])
        ids={r['chunk_id'] for r in selected}
        groups={}
        for r in ordered:
            groups.setdefault(r['evidence_group'],set()).add(r['chunk_id'])
        closed=all(set(r['requires'])<=ids and groups[r['evidence_group']]<=ids for r in selected)
        if not closed:
            raise ValueError('INCOMPLETE_CONTEXT_DEPENDENCY')
        required=gold[run['question_id']]['necessary_evidence']
        before=_union_covered_ids(run['response']['sources'],required)
        after=_union_covered_ids(selected,required)
        request=capacity._requests(*verifier.initial_messages(query,selected))[0].body
        rows.append(dict(question_id=run['question_id'],mode=run['plugin'],
            old_ids=trace['context_ids'],new_ids=[r['chunk_id'] for r in selected],
            old_coverage=len(before)/len(required),new_coverage=len(after)/len(required),
            gained_gold_indexes=sorted(after-before),lost_gold_indexes=sorted(before-after),
            dependencies_complete=closed,decision=decision,capacity=ct,
            initial_request_sha256=digest(request)))
    regressions=[r for r in rows if r['lost_gold_indexes']]
    result=dict(kind='offline_deterministic_context_replay_not_live_quality',implementation=implementation,
        rows=rows,records=len(rows),regressions=len(regressions),
        improvements=sum(bool(r['gained_gold_indexes']) for r in rows),
        exact_old_context_matches=sum(r['old_ids']==r['new_ids'] for r in rows))
    if implementation=='current':
        from run_b3_delivery_eval import source_hashes
        result['source_hashes']=source_hashes()
        baseline=read(TARGET/'offline-baseline-replay.json')
        if baseline['records']!=120 or baseline['exact_old_context_matches']!=120:
            raise ValueError('BASELINE_REPLAY_NOT_VERIFIED')
        q09=next(r for r in rows if r['question_id']=='tree-v1-09' and r['mode']=='b3_unit')
        result['gate_passed']=len(rows)==120 and not regressions and q09['new_coverage']==1
        result['q09_b3_coverage']=q09['new_coverage']
    name='offline-baseline-replay.json' if implementation=='baseline' else 'offline-replay.json'
    with (TARGET/name).open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k not in {'rows','source_hashes'}}),flush=True)
    if implementation=='current' and not result['gate_passed']:
        raise SystemExit(2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation',choices=('baseline','current'),required=True)
    execute(parser.parse_args().implementation)
