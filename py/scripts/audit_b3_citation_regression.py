"""Offline audit of saved results. No model calls, credentials, or source edits."""
from collections import Counter
from fractions import Fraction
from hashlib import sha256
import argparse
import ast
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

PY = Path(__file__).resolve().parents[1]
BASE = PY/'.rag-evaluation'
OLD = BASE/'tree-b3-repair-40-v2'
NEW = BASE/'tree-b3-delivery-repair-40-single-v1'
ARCHIVE = BASE/'tree-b3-delivery-repair-baseline-20260923'
OUT = NEW/'citation-reaudit'


def audit(event, args):
    if event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system'}:
        raise AssertionError('OFFLINE_ONLY')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if any(p.lower().startswith('.env') for p in Path(os.fsdecode(args[0])).parts):
            raise AssertionError('NO_CREDENTIAL_READ')


sys.dont_write_bytecode = True
sys.addaudithook(audit)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()]


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def save(name, value):
    OUT.mkdir(exist_ok=True)
    with (OUT/name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def coverage(record, gold, field):
    # Independent implementation: clip spans to each target, merge intervals,
    # and compare total union length with target length (exact rational macro).
    if not record.get('service_success'):
        return Fraction(0)
    items = record['response'][field]
    spans = [s for item in items for s in item['source_spans']]
    targets = gold['necessary_evidence']
    covered = 0
    for target in targets:
        intervals = sorted((max(s['start'], target['start']), min(s['end'], target['end']))
            for s in spans if all(s.get(k) == target.get(k) for k in
                ('material_version_id','artifact_hash','page','block'))
            and s['start'] < target['end'] and s['end'] > target['start'])
        merged = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        covered += sum(end-start for start,end in merged) == target['end']-target['start']
    return Fraction(covered, len(targets))


def request_hashes(implementation):
    root = ARCHIVE/'py' if implementation == 'old' else PY
    sys.path.insert(0, str(root))
    from knowpath_backend.learning.rag.capacity import AnswerCapacity
    from knowpath_backend.learning.rag.model_services import BudgetedJsonModel
    from knowpath_backend.learning.rag.token_budget import ConservativeByteProfile
    from knowpath_backend.learning.rag.verification import AnswerVerifier
    config = read((OLD if implementation == 'old' else NEW)/'freeze.json')['config']
    budgets, protocol = config['budgets'], config['prompts']['protocol']
    model = object.__new__(BudgetedJsonModel)
    model.settings = SimpleNamespace(chat_model=config['models']['chat_model'],
        chat_provider=config['models']['chat_provider'], context_budget_tokens=budgets['model_context_tokens'])
    model.max_input_tokens = budgets['model_input_tokens']
    model.max_output_tokens = budgets['model_output_tokens']
    model.max_draft_bytes = protocol['max_draft_bytes']
    model.response_format = protocol['response_format']
    cp = protocol['counting_profile']
    model.counting_profile = ConservativeByteProfile(cp['provider'], cp['model'], cp['per_message_overhead'], cp['request_overhead'])
    def forbidden(*args, **kwargs):
        raise AssertionError('NO_MODEL_CALL')
    model.generate_json = forbidden
    verifier = AnswerVerifier(model, model)
    capacity = AnswerCapacity(verifier, generation_seconds=10, verification_seconds=25)
    records = rows(OLD/'retry-two-extended-v1/merged.jsonl' if implementation == 'old' else NEW/'dev.jsonl')
    result = []
    for r in records:
        response = r.get('response') or {}
        trace = response.get('trace', {})
        request = None
        if response.get('sources') and trace.get('query'):
            request = capacity._requests(*verifier.initial_messages(trace['query']['query'],response['sources']))[0].body
        result.append(dict(question_id=r['question_id'], mode=r['plugin'], request_sha256=digest(request) if request else None,
            body_keys=sorted(request) if request else None,
            sampling={k:request[k] for k in ('temperature','top_p','seed') if k in request} if request else None,
            source_payload_sha256=digest(response.get('sources'))))
    save(implementation+'-requests.json', result)
    print(json.dumps(dict(implementation=implementation, rows=len(result), source_root=str(root))))


def compact_trace(record):
    response = record.get('response') or {}
    trace = response.get('trace',{})
    return dict(status=record['answer_status'], claims=len(response.get('claims', [])),
        delivery=trace.get('delivery'), contract_repairs=trace.get('contract_repairs'),
        drafts=[dict(status=d.get('status'), claims=len(d.get('claims',[])),
            missing_points=d.get('missing_points')) for d in trace.get('drafts',[])],
        verdicts=[dict(reason=v.get('reason_code'), complete=v.get('complete'),
            checks=[{k:c.get(k) for k in ('claim_id','status','issue_type','reason')} for c in v.get('checks',[])],
            requirements=[{k:c.get(k) for k in ('point_id','status','reason')} for c in v.get('requirement_checks',[])])
            for v in trace.get('verdicts',[])],
        failed_calls=[{k:c.get(k) for k in ('stage','validation','failure_kind','schema_rule','schema_subcategory','schema_error_path','schema_error_type')}
            for c in (trace.get('call_journal') or {}).get('calls',[]) if c.get('validation') == 'failed'])


def analyze():
    sys.path.insert(0,str(PY))
    from knowpath_backend.rag_eval.dataset import verify_freeze
    from knowpath_backend.rag_eval.scoring import evidence_coverage
    frozen, questions, _ = verify_freeze(NEW/'freeze.json')
    for entry in read(ARCHIVE/'path-map.json').values():
        assert file_hash(Path(entry['archive'])) == entry['sha256']
    assert file_hash(OLD/'dataset.jsonl') == file_hash(NEW/'dataset.jsonl')
    paths = dict(original=OLD/'dev.jsonl', retry6=OLD/'retry-six-v1/retries.jsonl',
        retry2=OLD/'retry-two-extended-v1/retries.jsonl', merged=OLD/'retry-two-extended-v1/merged.jsonl', current=NEW/'dev.jsonl')
    batches = {k:rows(p) for k,p in paths.items()}
    gold = {q['question_id']:q for q in questions}
    def key(r): return r['question_id'],r['plugin'],r['repeat']
    for label in ('original','merged','current'):
        assert len(batches[label]) == len({key(r) for r in batches[label]}) == 120
        assert Counter(r['plugin'] for r in batches[label]) == {'a':40,'b2_r1':40,'b3_unit':40}
    schedule = read(NEW/'schedule.json')
    assert [{k:r[k] for k in ('question_id','plugin','repeat','order')} for r in batches['current']] == schedule
    reconstructed = {key(r):r for r in batches['original']}
    for retry in batches['retry6']+batches['retry2']:
        assert reconstructed[key(retry)]['service_success'] is False
        reconstructed[key(retry)] = retry
    for r in batches['merged']:
        # Retry metadata may be removed by final merger; core fields are exact.
        assert all(r[k] == reconstructed[key(r)][k] for k in batches['original'][0])
    totals = {}
    for label in ('original','merged','current'):
        totals[label] = {}
        for mode in ('a','b2_r1','b3_unit'):
            selected = [r for r in batches[label] if r['plugin']==mode]
            values = {}
            for field in ('sources','citations'):
                scores = [coverage(r,gold[r['question_id']],field) for r in selected]
                for r, score in zip(selected,scores):
                    if r['service_success']:
                        assert abs(float(score)-evidence_coverage(r['response'],gold[r['question_id']],field)) < 1e-12
                values[field] = float(sum(scores)/40)
            values['status'] = dict(Counter(r['answer_status'] if r['service_success'] else 'failed' for r in selected))
            totals[label][mode] = values
    current_analysis = read(NEW/'delivery-analysis.json')
    assert current_analysis['input_sha256'] == digest(batches['current'])
    original_totals = read(OLD/'retry-two-extended-v1/totals.json')['totals']
    for mode in ('a','b2_r1','b3_unit'):
        assert abs(totals['merged'][mode]['citations']-original_totals[mode]['citation']) < 1e-12
        assert abs(totals['current'][mode]['citations']-current_analysis['modes'][mode]['metrics']['citation_coverage']['mean']) < 1e-12
    oldmap = {r['question_id']:r for r in batches['merged'] if r['plugin']=='b3_unit'}
    newmap = {r['question_id']:r for r in batches['current'] if r['plugin']=='b3_unit'}
    originalmap = {r['question_id']:r for r in batches['original'] if r['plugin']=='b3_unit'}
    oldrequests = {r['question_id']:r for r in read(OUT/'old-requests.json') if r['mode']=='b3_unit'}
    newrequests = {r['question_id']:r for r in read(OUT/'new-requests.json') if r['mode']=='b3_unit'}
    ledger = []
    for qid in sorted(gold):
        before = coverage(oldmap[qid],gold[qid],'citations')
        after = coverage(newmap[qid],gold[qid],'citations')
        original = coverage(originalmap[qid],gold[qid],'citations')
        same = oldrequests[qid]['request_sha256'] == newrequests[qid]['request_sha256']
        if before != after:
            ledger.append(dict(question_id=qid, old=float(before), new=float(after), original_single=float(original),
                delta_pp=float((after-before)*100/40), retry_gain_pp=float((before-original)*100/40),
                initial_request_identical=same, old_trace=compact_trace(oldmap[qid]), new_trace=compact_trace(newmap[qid])))
    sameq = [q for q in gold if oldrequests[q]['request_sha256']==newrequests[q]['request_sha256']]
    groups = {}
    for kind, ids in [('contract',['05','14','18','20']), ('semantic_empty',['37','39']), ('partial_coverage',['07','17']), ('gain',['01'])]:
        selected = [r for r in ledger if r['question_id'][-2:] in ids]
        groups[kind] = dict(questions=[r['question_id'] for r in selected], delta_pp=sum(r['delta_pp'] for r in selected))
    result = dict(freeze_id=frozen['freeze_id'], hashes={k:file_hash(p) for k,p in paths.items()},
        dataset_sha256=file_hash(NEW/'dataset.jsonl'), integrity_verified=True, independent_rescore_matches=True,
        totals=totals, same_b3_initial_requests=len(sameq), different_b3_initial_requests=sorted(set(gold)-set(sameq)),
        current_sampling=read(OUT/'new-requests.json')[0]['sampling'], groups=groups, ledger=ledger,
        limitation='Stored failed provider payloads are unavailable. No exact replay of rejected raw responses; stochastic cause remains unproven.')
    save('reaudit.json', result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('ledger','hashes')},ensure_ascii=True))


def rules():
    sys.path.insert(0,str(PY))
    from knowpath_backend.learning.rag.verification import numeric_values
    paths = [ARCHIVE/'py/knowpath_backend/learning/rag/verification.py',
             PY/'knowpath_backend/learning/rag/verification.py']
    trees = [ast.parse(p.read_text(encoding='utf-8')) for p in paths]
    nodes = [{n.name:n for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.ClassDef))} for t in trees]
    same_ast = {name:ast.dump(nodes[0][name])==ast.dump(nodes[1][name])
                for name in ('numeric_values','_messages','WireDraft','WireVerdict')}
    def citation_condition(tree):
        matches = [ast.dump(n.test) for n in ast.walk(tree) if isinstance(n,ast.If)
            and 'next(' in ast.unparse(n.test) and 'draft.claims' in ast.unparse(n.test)
            and 'citation_ids' in ast.unparse(n.test)]
        assert len(matches)==1
        return matches[0]
    same_ast['supported_citations_must_equal_draft_condition'] = citation_condition(trees[0]) == citation_condition(trees[1])
    rows_by = {r['question_id']:r for r in rows(NEW/'dev.jsonl') if r['plugin']=='b3_unit'}
    findings = []
    for qid in ('tree-v1-07','tree-v1-37','tree-v1-39'):
        r=rows_by[qid]; response=r['response']; trace=response['trace']
        source = {s['chunk_id']:s['source_text'] for s in response['sources']}
        rounds=[]
        assert len(trace['drafts']) == len(trace['verdicts'])
        for draft, verdict in zip(trace['drafts'],trace['verdicts']):
            by={c['claim_id']:c for c in verdict['checks']}
            retained=set(); details=[]
            for claim in draft['claims']:
                check=by[claim['claim_id']]
                provenance='\n'.join(source[s['citation_id']][s['start']:s['end']] for s in check.get('evidence_spans',[]))
                values=numeric_values(claim['text']); evidence_values=numeric_values(provenance)
                blocked_deps=sorted(set(claim['depends_on'])-retained)
                keep=check['status']=='supported' and not blocked_deps
                if keep: retained.add(claim['claim_id'])
                details.append(dict(claim_id=claim['claim_id'],text=claim['text'],kind=claim['kind'],
                    status_after_local_checks=check['status'],reason=check.get('reason'),
                    depends_on=claim['depends_on'],blocked_dependencies=blocked_deps,retained=keep,
                    numeric_values=sorted(map(str,values)),evidence_numeric_values=sorted(map(str,evidence_values)),
                    numeric_mismatch=claim['kind']=='fact' and not values <= evidence_values,
                    provenance=provenance))
            rounds.append(dict(retained_claim_ids=sorted(retained),details=details))
        assert rounds[-1]['retained_claim_ids']==sorted(c['claim_id'] for c in response['claims'])
        findings.append(dict(question_id=qid,rounds=rounds))
    result=dict(unchanged_ast=same_ast,
        numeric_probes={s:sorted(map(str,numeric_values(s))) for s in ('二元对举','s11','唯一标准','父母在，不远游，游必有方。')},
        findings=findings,
        scope='Replays deterministic numeric extraction/dependency pruning from saved normalized traces; not rejected raw provider payload replay.')
    save('rule-audit.json',result)
    print(json.dumps(dict(unchanged_ast=same_ast,numeric_probes=result['numeric_probes'],
        retained_by_round={f['question_id']:[r['retained_claim_ids'] for r in f['rounds']] for f in findings}),ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('old','new','analyze','rules'))
    args = parser.parse_args()
    if args.stage=='analyze':
        analyze()
    elif args.stage=='rules':
        rules()
    else:
        request_hashes(args.stage)
