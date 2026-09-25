"""Offline result tables, structural gates and mode-blind external review pack."""
from hashlib import sha256
import json
from pathlib import Path
import random
from statistics import mean
import sys

PY=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PY))
sys.path.insert(0,str(PY/'scripts'))
from knowpath_backend.rag_eval.scoring import _union_covered_ids
from run_b3_delivery_eval import TARGET, MODES, read, write


def full_unit_recall(ids, required, units, k):
    relevant=[u for u in units if len(u['leaf_ids'])>1 and _union_covered_ids([u],required)]
    if not relevant:
        return None
    selected=set(ids[:k])
    return sum(set(u['leaf_ids'])<=selected for u in relevant)/len(relevant)


def blind_review(records,questions):
    gold={q['question_id']:q for q in questions}
    ordered=list(records)
    random.Random(20260923).shuffle(ordered)
    package,mapping=[],{}
    for i,r in enumerate(ordered,1):
        response=r.get('response') or {}
        identifier=f'review-{i:04}'
        package.append(dict(review_id=identifier,question=gold[r['question_id']]['question'],
            answer=response.get('text'),claims=response.get('claims',[]),sources=response.get('sources',[]),
            citations=response.get('citations',[]),human_correctness=None,human_citation_support=None,
            reviewer_notes=None))
        mapping[identifier]={k:r[k] for k in ('question_id','plugin','repeat')}
    return package,mapping


def structural_gates(analysis,records,questions,units,audit):
    by={(r['question_id'],r['plugin'],r['repeat']):r for r in records}
    positive=set(audit['continuation_gold_question_ids'])
    negative={q['question_id'] for q in questions if q.get('relation_type') in {'single_leaf','adjacency_negative','adjacent_noise'}}
    per=analysis['per_question']
    subset={}
    for label,ids in [('continuation_audited',positive),('frozen_negative_labels',negative)]:
        subset[label]={m:{metric:(mean([per[m][q][metric] for q in sorted(ids)])
                       if ids and all(per[m][q][metric] is not None for q in ids) else None)
                        for metric in ('context_coverage','citation_coverage')} for m in MODES}
    full_units={}
    for mode in MODES:
        values=[]
        for q in questions:
            if q['question_id'] not in positive:
                continue
            repetitions=[]
            for repeat in range(3):
                r=by.get((q['question_id'],mode,repeat))
                trace=((r or {}).get('response') or {}).get('trace',{})
                repetitions.append(full_unit_recall(trace['reranked_ids'],q['necessary_evidence'],units,10)
                                   if 'reranked_ids' in trace else None)
            values.append(mean(repetitions) if all(v is not None for v in repetitions) else None)
        full_units[mode]=mean(values) if values and all(v is not None for v in values) else None
    def delta(metric,group=None):
        if group:
            a,b=subset[group]['a'][metric],subset[group]['b3_unit'][metric]
        else:
            return analysis['comparisons']['b3_unit-a'][metric]['difference']
        return b-a if a is not None and b is not None else None
    def minimum(value,threshold):
        return value>=threshold-1e-12 if value is not None else None
    retention=[]; expansions=[]
    for q in questions:
        for repeat in range(3):
            r=by.get((q['question_id'],'b3_unit',repeat))
            retrieval=((r or {}).get('response') or {}).get('trace',{}).get('retrieval',{})
            retention.append(retrieval.get('a_retention_at_40'))
            if q['question_id'] in negative:
                expansions.append(retrieval.get('structural_additions'))
    unit_gain=full_units['b3_unit']-full_units['a'] if all(full_units[m] is not None for m in ('a','b3_unit')) else None
    lat={m:analysis['modes'][m]['latency_ms'] for m in MODES}
    ratio=lat['b3_unit']['p95']/lat['a']['p95'] if all(lat[m]['complete'] for m in ('a','b3_unit')) and lat['a']['p95'] else None
    negative_equal=all(per['a'][q][metric] is not None and per['b3_unit'][q][metric] is not None
                       and abs(per['a'][q][metric]-per['b3_unit'][q][metric])<1e-12
                       for q in negative for metric in ('context_coverage','citation_coverage'))
    modes=analysis['modes']
    calls_equal=all(len({modes[m]['usage']['calls_by_stage'].get(stage,0) for m in MODES})==1
                    for stage in ('embedding','reranking'))
    known_calls=all(modes[m]['missing']==0 and modes[m]['usage']['unknown_journals']==0 for m in MODES)
    checks=dict(overall_context_gain_at_least_3pp=minimum(delta('context_coverage'),.03),
        continuation_full_unit_recall10_gain_at_least_3pp=minimum(unit_gain,.03),
        continuation_context_gain_at_least_3pp=minimum(delta('context_coverage','continuation_audited'),.03),
        continuation_citation_gain_at_least_3pp=minimum(delta('citation_coverage','continuation_audited'),.03),
        legacy_ndcg10_regression_at_most_1pp=minimum(delta('reranked_legacy_ndcg_at_10'),-.01),
        negative_context_and_citation_unchanged=negative_equal,
        negative_zero_structural_expansion=(all(v==0 for v in expansions) if all(v is not None for v in expansions) else None),
        retention_at_least_98pct=(min(retention)>=.98 if all(v is not None for v in retention) else None),
        p95_ratio_at_most_1_2=(ratio<=1.2 if ratio is not None else None),
        provider_failures_zero=(all(modes[m]['provider_failure_requests']==0 for m in MODES) if known_calls else None),
        equal_embedding_rerank_counts=(calls_equal if known_calls else None),human_review_approved=None)
    return dict(checks=checks,subsets=subset,continuation_question_ids=sorted(positive),
        frozen_negative_question_ids=sorted(negative),label_caveat=audit['single_leaf_labels_requiring_cross_leaf_coverage'],
        full_unit_recall10=full_units,p95_ratio=ratio,
        unit_definition='fraction of multi-leaf units covering at least one full necessary span with every leaf present in top10',
        decision='no_automatic_adoption_human_quality_unknown')


def verify_analysis_records(analysis,records):
    actual=sha256(json.dumps(records,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if analysis.get('input_sha256')!=actual:
        raise ValueError('ANALYSIS_RESULTS_CHANGED')


def build():
    from knowpath_backend.rag_eval.dataset import verify_freeze
    verify_freeze(TARGET/'freeze.json')
    records=[json.loads(line) for line in (TARGET/'dev.jsonl').read_text(encoding='utf-8').splitlines()]
    questions=[json.loads(line) for line in (TARGET/'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    analysis=read(TARGET/'delivery-analysis.json')
    verify_analysis_records(analysis,records)
    gates=structural_gates(analysis,records,questions,read(TARGET/'unit-manifest.json')['units'],read(TARGET/'structure-audit.json'))
    write(TARGET/'delivery-gates.json',gates)
    package,mapping=blind_review(records,questions)
    write(TARGET/'blind-review.json',package)
    write(TARGET/'blind-review-key.json',mapping)
    lines=['# B3 交付修复测评结果','',f"实际记录 {len(records)}/360；40 道题、3 模式、每模式重复 3 次。",'',
        '人工正确性未知。引用覆盖是坐标覆盖，不代表答案正确；历史选择性重跑结果只作背景。','',
        '| 指标 | A | B2-R1 | B3 |','|---|---:|---:|---:|']
    metrics=[('候选联合 Recall@40','candidate_union_recall_at_40'),('重排联合 Recall@3','reranked_union_recall_at_3'),
        ('重排联合 Recall@10','reranked_union_recall_at_10'),('旧口径 NDCG@10','reranked_legacy_ndcg_at_10'),
        ('上下文必要证据覆盖','context_coverage'),('引用必要证据覆盖','citation_coverage')]
    for label,key in metrics:
        values=[analysis['modes'][m]['metrics'][key]['mean'] for m in MODES]
        lines.append('| '+label+' | '+' | '.join('unknown' if v is None else f'{v:.6f}' for v in values)+' |')
    lines+=['','| 交付/稳定性 | A | B2-R1 | B3 |','|---|---:|---:|---:|']
    for label,key in [('流程完成数','service_successes'),('非空可信交付数','nonempty_verified_delivery'),
                      ('发生契约错误','contract_error_requests'),('恢复部分答案','recovered_partial'),
                      ('契约失败空答案','contract_empty_answers'),('请求失败','failed')]:
        lines.append('| '+label+' | '+' | '.join(str(analysis['modes'][m][key]) for m in MODES)+' |')
    lines+=['','## B3 − A 配对差值（95% 探索性区间）','']
    for label,key in metrics:
        pair=analysis['comparisons']['b3_unit-a'][key]
        lines.append(f"- {label}：{pair['difference']}；区间 {pair['ci95']}")
    lines+=['','## 原定门槛','']
    for name,value in gates['checks'].items():
        lines.append(f'- {name}: '+('unknown' if value is None else '通过' if value else '未通过'))
    lines+=['','第 03/10 题原负例标签与原文多叶结构不一致；冻结标签与结构审计均保留。',
        '全部逐题、时延、usage、未知分母及区间见 delivery-analysis.json；盲审材料不带模式标签。',
        '费用因冻结价格表缺失保持 unknown，不能解释为零费用。','']
    path=TARGET/'results.md'
    with path.open('x',encoding='utf-8') as stream:
        stream.write('\n'.join(lines))
    print(json.dumps(dict(report=str(path),records=len(records))),flush=True)


if __name__=='__main__':
    build()
