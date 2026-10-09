"""Read-only audit of frozen inputs; write derived artifacts only next to this script."""
import collections, hashlib, json, math, pathlib, statistics, sys
sys.stdout.reconfigure(encoding="utf-8")
OUT = pathlib.Path(__file__).resolve().parent
ROOT = OUT.parents[2]
SRC = ROOT / ".worktrees/rag-foundation/py/.rag-evaluation/tree-b4-final-40-v1"
MODES = ("a0", "f", "b4")
KS = (1, 3, 5, 10, 20, 40)
IDENTITY = ("material_version_id", "artifact_hash", "page", "block")
def read(name): return json.loads((SRC/name).read_text(encoding="utf-8"))
def lines(name): return [json.loads(s) for s in (SRC/name).read_text(encoding="utf-8").splitlines() if s.strip()]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def digest(o): return hashlib.sha256(json.dumps(o,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()).hexdigest()
def save(name, data): (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
def avg(v): return statistics.mean(v) if v else None
def quant(v,q): return sorted(v)[max(0,math.ceil(len(v)*q)-1)] if v else None
def dist(v): return dict(n=len(v),mean=avg(v),min=min(v) if v else None,max=max(v) if v else None,**{f"p{k}":quant(v,k/100) for k in (50,90,95,99)})
def wilson(k,n):
    if not n: return None
    z=1.95996398454;p=k/n;d=1+z*z/n;c=(p+z*z/(2*n))/d;w=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [c-w,c+w]
def same(a,b): return all(a.get(k)==b.get(k) for k in IDENTITY)
def covers(a,b): return same(a,b) and a["start"]<=b["start"] and a["end"]>=b["end"]
def union_ids(items,gold):
    found=set()
    for i,g in enumerate(gold):
        cursor=g["start"]
        for start,end in sorted((s["start"],s["end"]) for item in items for s in item["source_spans"] if same(s,g)):
            if start>cursor: break
            cursor=max(cursor,end)
            if cursor>=g["end"]: found.add(i);break
    return found
def valid(items):
    return isinstance(items,list) and all(isinstance(x,dict) and isinstance(x.get("source_spans"),list) and x["source_spans"] and all(all(k in s for k in IDENTITY) and type(s.get("start")) is int and type(s.get("end")) is int and 0<=s["start"]<s["end"] for s in x["source_spans"]) for x in items)
def sources(r,field):
    snap=r.get("retrieval_snapshot") or {};trace=((r.get("response") or {}).get("trace") or {})
    value=snap.get(field)
    if field!="context_sources" and field not in snap: value=trace.get(field)
    return value if valid(value) else None
def ranking(items,gold,prefix):
    if items is None: return {}
    relevant=[any(covers(s,g) for s in item["source_spans"] for g in gold) for item in items]
    rank=next((i+1 for i,v in enumerate(relevant) if v),None)
    out={prefix+"_legacy_mrr":1/rank if rank else 0.0,prefix+"_list_length":len(items)}
    for k in KS:
        singles={i for i,g in enumerate(gold) if any(covers(s,g) for item in items[:k] for s in item["source_spans"])}
        united=union_ids(items[:k],gold)
        out[prefix+f"_legacy_recall_at_{k}"]=len(singles)/len(gold)
        out[prefix+f"_union_recall_at_{k}"]=len(united)/len(gold)
        out[prefix+f"_hit_at_{k}"]=float(bool(united))
        out[prefix+f"_all_gold_at_{k}"]=float(len(united)==len(gold))
        out[prefix+f"_precision_at_{k}"]=sum(relevant[:k])/k
    out[prefix+"_legacy_ndcg_at_10"]=sum(1/math.log2(i+2) for i,v in enumerate(relevant[:10]) if v)/sum(1/math.log2(i+2) for i in range(min(10,len(gold))))
    out[prefix+"_all_necessary"]=float(len(union_ids(items,gold))==len(gold))
    return out
questions=lines("dataset.jsonl"); records=lines("dev.jsonl"); old=read("analysis.json")
assert len(questions)==40 and len(records)==120
Q={q["question_id"]:q for q in questions}; assert len(Q)==40
keys=[(r["question_id"],r["plugin"],r["repeat"]) for r in records]
assert len(set(keys))==120 and set(keys)=={(q,m,0) for q in Q for m in MODES}
schedule=read("schedule.json"); assert keys==[(r["question_id"],r["plugin"],r["repeat"]) for r in schedule]
prev=None
for r in records:
    assert r["previous_record_hash"]==prev and r["record_hash"]==digest({k:v for k,v in r.items() if k!="record_hash"})
    prev=r["record_hash"]
gold_checked=0
for q in questions:
    for g in q["necessary_evidence"]:
        p=SRC/g["artifact_path"];text=p.read_text(encoding="utf-8")
        assert sha(p)==g["artifact_hash"] and text[g["start"]:g["end"]]==g["quote"]
        gold_checked+=1
per={m:{} for m in MODES}; consistency=0
for r in records:
    q=Q[r["question_id"]];gold=q["necessary_evidence"];resp=r.get("response") or {};trace=resp.get("trace") or {}
    delivered=bool(r.get("service_success") and r.get("answer_status") in ("answered","partial") and resp.get("claims"))
    v={}
    for prefix,field in (("candidate","candidate_sources"),("reranked","reranked_sources")):v.update(ranking(sources(r,field),gold,prefix))
    ctx=sources(r,"context_sources");cit=resp.get("citations")
    v["context_coverage"]=len(union_ids(ctx,gold))/len(gold) if ctx is not None else None
    v["citation_coverage"]=len(union_ids(cit,gold))/len(gold) if delivered and valid(cit) else (None if delivered else 0.0)
    for pre in ("context","citation"): v[pre+"_all_necessary"]=None if v[pre+"_coverage"] is None else float(v[pre+"_coverage"]==1)
    v["nonempty_verified_delivery"]=float(delivered);v["complete_answer"]=float(delivered and r["answer_status"]=="answered")
    for k,x in old["per_question"][r["plugin"]][r["question_id"]].items():
        y=v.get(k)
        assert (x is None and y is None) or (x is not None and y is not None and abs(x-y)<1e-10),(r["question_id"],r["plugin"],k,x,y)
        consistency+=1
    journal=trace.get("call_journal",r.get("call_journal"))
    calls=journal.get("calls") if isinstance(journal,dict) else None
    stage={}
    if isinstance(calls,list):
        for c in calls:
            if isinstance(c.get("elapsed_ms"),(int,float)): stage.setdefault(c.get("stage","unknown"),[]).append(c["elapsed_ms"])
    answer=resp.get("text") or "";claims=resp.get("claims") or []
    v.update(question_id=q["question_id"],domain=q["domain"],service_success=bool(r.get("service_success")),status=r.get("answer_status") or ("unknown" if r.get("attempt_status")=="unknown" else "failed"),latency_ms=r.get("latency_ms"),error=r.get("error_code"),answer_chars=len(answer),claims_count=len(claims),cited_claims_count=sum(bool(c.get("citation_ids")) for c in claims),citation_count=len(cit) if isinstance(cit,list) else None,revisions=trace.get("revisions"),stage_call_sum_ms={k:sum(vals) for k,vals in stage.items()},journal_present=isinstance(calls,list))
    per[r["plugin"]][q["question_id"]]=v
metric_names=sorted({k for m in MODES for v in per[m].values() for k in v if any(k.startswith(p) for p in ("candidate_","reranked_"))}|{"context_coverage","citation_coverage","context_all_necessary","citation_all_necessary"})
summaries={}
for m in MODES:
    rows=list(per[m].values());s={}
    s["scheduled"]=40;s["status_counts"]=dict(collections.Counter(v["status"] for v in rows))
    n=sum(v["service_success"] for v in rows);s["service_success"]={"count":n,"rate":n/40,"wilson95":wilson(n,40)}
    s["substantive_delivery"]={"count":sum(v["nonempty_verified_delivery"] for v in rows),"rate":sum(v["nonempty_verified_delivery"] for v in rows)/40}
    s["rank_and_coverage"]={}
    for k in metric_names:
        vals=[v[k] for v in rows if isinstance(v.get(k),(float,int))]
        summary={"observed_mean":avg(vals),"observed_n":len(vals),"unknown_n":40-len(vals),"scheduled_mean":avg(vals) if len(vals)==40 else None}
        if vals and not k.endswith("list_length") and "ndcg" not in k:
            summary["all40_lower_bound"]=sum(vals)/40;summary["all40_upper_bound"]=(sum(vals)+40-len(vals))/40
        s["rank_and_coverage"][k]=summary
    s["latency_ms_all_observed"]=dist([v["latency_ms"] for v in rows if isinstance(v["latency_ms"],(float,int))])
    s["latency_ms_service_success"]=dist([v["latency_ms"] for v in rows if v["service_success"] and isinstance(v["latency_ms"],(float,int))])
    s["latency_ms_substantive_delivery"]=dist([v["latency_ms"] for v in rows if v["nonempty_verified_delivery"] and isinstance(v["latency_ms"],(float,int))])
    assert abs(s["latency_ms_all_observed"]["p50"]-old["modes"][m]["latency_ms"]["p50"])<1e-8
    assert abs(s["latency_ms_all_observed"]["p95"]-old["modes"][m]["latency_ms"]["p95"])<1e-8
    stages=sorted({stage for v in rows for stage in v["stage_call_sum_ms"]})
    s["stage_observed_call_sum_ms"]={stage:dist([v["stage_call_sum_ms"][stage] for v in rows if stage in v["stage_call_sum_ms"]]) for stage in stages}
    s["retrieval_dependency_call_sum_ms"]=dist([sum(v["stage_call_sum_ms"].get(k,0) for k in ("retrieval","embedding","reranking","navigation")) for v in rows if v.get("context_coverage") is not None and v["journal_present"]])
    s["substantive_answer_chars"]=dist([v["answer_chars"] for v in rows if v["nonempty_verified_delivery"]])
    s["claims_total"]=sum(v["claims_count"] for v in rows);s["claims_with_citation_id"]=sum(v["cited_claims_count"] for v in rows)
    s["claim_citation_id_rate"]=s["claims_with_citation_id"]/s["claims_total"] if s["claims_total"] else None
    s["requests_with_revision"]=sum((v["revisions"] or 0)>0 for v in rows);s["revision_observed_n"]=sum(v["revisions"] is not None for v in rows)
    s["original_usage_accounting"]=old["modes"][m]["usage"]
    s["original_contract_counts"]={k:old["modes"][m].get(k) for k in ("contract_error_requests","contract_empty_answers","recovered_partial","provider_failure_requests","source_violations")}
    s["by_domain"]={}
    for domain in sorted({q["domain"] for q in questions}):
        group=[v for v in rows if v["domain"]==domain]
        s["by_domain"][domain]={"n":len(group),"service_success":sum(v["service_success"] for v in group),"substantive_delivery":sum(v["nonempty_verified_delivery"] for v in group),"answered":sum(v["status"]=="answered" for v in group),"citation_coverage":avg([v["citation_coverage"] for v in group]),"context_coverage_observed":avg([v["context_coverage"] for v in group if v["context_coverage"] is not None])}
    summaries[m]=s
common=[q for q in Q if all(per[m][q].get("context_coverage") is not None and per[m][q].get("candidate_legacy_mrr") is not None and per[m][q].get("reranked_legacy_mrr") is not None for m in MODES)]
paired={m:{k:avg([per[m][q].get(k) for q in common]) for k in metric_names if all(per[m][q].get(k) is not None for q in common)} for m in MODES}
# Independent single-AI retrospective review. Judgments concern visible question + original gold excerpts.
# They are NOT human review, blinded grading, a second model run, or claim-level entailment certification.
complete={6,7,8,9,10,11,12,15,16,17,19,21,22,23,24,26,27,28,29,30,31,32,33,34,37}
partial={5,14,18,36};abstain={13,20,25,35};failed={1,2,3,38,39,40};unknown={4}
flags={9:"最后一段将严重欺凌规定误写为第四十条，实际为第三十九条；主要处置要求正确。",
15:"将“只有一个元素的张量”直接等同“零阶张量”缺少维数限定；单元素向量或矩阵也可能只有一个元素。",
33:"由“知之次也”进一步断言次于“生而知之”，属可争议解释，需引用原文或注解支持；核心诚实求知要求已答对。",
36:"“唯一现存载体”“无实祭之羊”不能由给定章句直接推出；同时遗漏相邻“射不主皮”的关联。",
37:"把义利对举扩大为“二者不可兼容”等绝对结论，超出给定章句能直接支持的范围。"}
notes={5:"主要受托资格已答；未覆盖无正当理由不得委托、每周联系及异常干预等。",
7:"回答禁体罚并概括第二十六条；不强行将相邻条文当成法定例外，保留关系解释争议。",
8:"符合题目登记劝返及书面报告要求；金标准还列禁止违规开除，答文未显式写出。",
10:"题目仅问报告部门，答文满足；金标准额外要求第四十条补充关系，不宜据此判错。",
11:"回答法定节假日及身份证件要求；金标准额外列禁入限入标志，答文未展开。",
14:"实名与22:00—08:00已答；未答第七十四条防沉迷补充。",
18:"仅说明shape，未完整说明向量维度是长度、张量维度是轴数的区别。",
20:"无实质答案；题目问dot，但第二条金标准证据是矩阵排布公式，标注存在不匹配。",
21:"虽标partial，正文已回答元素换位、形状交换和索引联系。",
23:"已答不改变形状及shape解释；未复述金标准代码的5×4示例，按提问主旨接受。",
24:"已答标量逐元素作用及形状不变；附加示例含义已说明。题目锚点仅代码围栏，金标准A*B未明确是标量。",
26:"已答累积总和且不降维；金标准额外示例要求未出现在问题中。",
27:"虽标partial，已完整回答按元素相乘后求和，并给出np.sum示例。",
32:"核心原文正确；“殆”的多种解释不在本次单句原文核验能力内。",
36:flags[36]}
review=[]
for i,q in enumerate(questions,1):
    label="complete" if i in complete else "partial" if i in partial else "abstain" if i in abstain else "failed" if i in failed else "unknown"
    assert i in complete|partial|abstain|failed|unknown
    review.append({"question_id":q["question_id"],"domain":q["domain"],"question_completion":label,"risk_flag":i in flags,"acceptable_complete":i in complete and i not in flags,"note":notes.get(i,flags.get(i,"已覆盖题目核心要求，所给原文未见明显冲突。" if i in complete else "没有可供评分的实质答复。"))})
reviewmeta={"reviewer":"当前 Codex 会话，单一 AI 人工式逐题复核","review_date":"2026-10-05","scope":"A0 原始40题答卷；非盲评；没有独立人工复核；对已提供原文作定性核对，不是逐claim全量事实验证","rubric":"complete=明确回答题目各核心子问；partial=有正确内容但漏核心子问；abstain=没有实质答案；failed=调用失败；unknown=终局未知。风险包括明确错误与需复核过度推断，不能统称幻觉。acceptable_complete=complete且本次未发现风险标记。","rows":review}
counts=collections.Counter(x["question_completion"] for x in review);passed=sum(x["acceptable_complete"] for x in review)
reviewmeta["summary"]={"completion_counts":dict(counts),"question_complete":len(complete),"acceptable_complete":passed,"acceptable_complete_all40":passed/40,"acceptable_complete_delivered29":passed/29,"risk_flagged_answers":len(flags),"risk_fraction_delivered":len(flags)/29,"descriptive_wilson95_acceptable_all40":wilson(passed,40),"note_ci":"只表示把40题当独立伯努利观测时的描述性区间，不涵盖标注偏差、AI评审误差、开发集偏差或同题生成方差"}
reviewmeta["by_domain"]={d:{"n":sum(x["domain"]==d for x in review),"acceptable_complete":sum(x["domain"]==d and x["acceptable_complete"] for x in review),"question_complete":sum(x["domain"]==d and x["question_completion"]=="complete" for x in review)} for d in sorted({q["domain"] for q in questions})}
meta={"audit_date":"2026-10-05","experiment_first_utc":records[0]["started_at"],"experiment_last_utc":records[-1]["completed_at"],"historical_run_not_live_retest":True,"source":str(SRC),"input_sha256":{name:sha(SRC/name) for name in ("dataset.jsonl","dev.jsonl","analysis.json","schedule.json","development.json")},"checks":{"unique_scheduled_records":120,"record_hash_chain":True,"gold_spans_hash_and_text_validated":gold_checked,"recomputed_original_metric_values":consistency,"original_p50_p95_match":True},"models":read("development.json")["models"],"human_gold_review_status":dict(collections.Counter(q["human_review_status"] for q in questions)),"expected_status":dict(collections.Counter(q["expected_answer_status"] for q in questions))}
result={"metadata":meta,"modes":summaries,"common_observed_questions":common,"paired_metrics_n31":paired,"per_question":per,"ai_semantic_review_a0":reviewmeta["summary"],"metric_notes":{"recall":"证据span坐标并集完整覆盖，逐题宏平均；另保留单叶口径","precision":"前K条叶子中完整覆盖至少一条gold span的比例，分母K；不是语义precision","ndcg":"历史实现：候选叶子二元相关性DCG，IDCG按gold span数构造；叶子与span计数单位不同，重复相关叶可能使其超过1。仅用于历史比较，不当成标准无偏NDCG。","latency":"nearest-rank分位数；毫秒；全体可测耗时包含快速失败。另给service_success及实质交付子集。非HTTP/UI/TTFT。","stage":"每请求已记录下游调用耗时求和，再取分位；只含该stage有记录的请求；不是完整阶段wall-clock。","bounds":"缺失观测按0或1得到全40题数学界限，不是补测值，也不是置信区间。"}}
save("metrics.json",result);save("ai-review-a0.json",reviewmeta)
def pct(x):return "unknown" if x is None else f"{x*100:.2f}%"
def num(x):return "unknown" if x is None else f"{x:.4f}"
def table(headers,rows):return "\n".join(["| "+" | ".join(headers)+" |","| "+" | ".join("---" for _ in headers)+" |",*["| "+" | ".join(map(str,r))+" |" for r in rows]])
report=["# RAG与40题答卷指标审计（2026-10-05）",
"本次从既有真实模型记录重新计算，不是2026-10-05在线重跑。原实验为2026-09-24 UTC 10:48—12:11（北京时间18:48—20:11），40题×A0/F/B4各一次，共120条。未合并network-recovery补跑数据。A0为该实验默认基线；F/B4为实验对照。调用的是后端RAG评测链路，不代表浏览器到HTTP服务的完整用户时延。",
"模型：qwen-plus；embedding：text-embedding-v3（1024维）；reranker：gte-rerank-v2。题目分布：法律14、线性代数13、论语13，全部预期answered；金标准human_review_status全部pending。这是已见开发集，不是独立盲测。",
"## 校验",
f"120条唯一执行键、预定顺序和记录哈希链通过；{gold_checked}段gold原文哈希/坐标/引文通过；{consistency}项原报告逐题指标复算一致；原P50/P95复现。原始文件未改动。metrics.json保留输入SHA-256、全部指标和120条逐题汇总。",
"## 三组共同可观测检索题集",
f"n={len(common)}，按三组检索快照共同完整性选取，不按答对与否筛选。只作同题横向比较，不能冒充完整40题质量。",
table(["指标","A0","F","B4"],[[f"{pre} 联合Recall@{k}",*[pct(paired[m][f"{pre}_union_recall_at_{k}"]) for m in MODES]] for pre in ("candidate","reranked") for k in KS]+[[f"{pre} {metric}",*[num(paired[m][f"{pre}_legacy_{metric}"]) for m in MODES]] for pre in ("candidate","reranked") for metric in ("mrr","ndcg_at_10")]),
"Recall为同源原文坐标并集完整覆盖的题宏平均。MRR要求单叶完整覆盖至少一段gold。NDCG使用历史自定义口径：叶子DCG/gold span数IDCG，存在单位不一致和重复计数风险，不等于严格标准NDCG；当前数据中超1的逐题个数见下。",
table(["模式","候选NDCG>1题数","重排NDCG>1题数"],[[m,*[sum(v.get(pre+"_legacy_ndcg_at_10",0)>1+1e-10 for v in per[m].values()) for pre in ("candidate","reranked")]] for m in MODES]),
"## 各方案全部观测及未知分母",
table(["指标","A0","F","B4"],[[k,*[f"{pct(summaries[m]['rank_and_coverage'][k]['observed_mean'])} ({summaries[m]['rank_and_coverage'][k]['observed_n']}/40)" for m in MODES]] for k in ("candidate_union_recall_at_3","candidate_union_recall_at_5","candidate_union_recall_at_10","reranked_union_recall_at_3","reranked_union_recall_at_5","reranked_union_recall_at_10","context_coverage","citation_coverage")]),
"不同方案可测题集不完全相同；完整40题检索均值仍unknown。不能把未观测检索当零检索质量，也不能把33题均值标为40题均值。metrics.json另给数学上下界。",
"## 全40题端到端交付",
table(["指标","A0","F","B4"],[
["服务正常返回",*[f"{summaries[m]['service_success']['count']}/40 ({pct(summaries[m]['service_success']['rate'])})" for m in MODES]],
["有实质已核验内容",*[f"{int(summaries[m]['substantive_delivery']['count'])}/40 ({pct(summaries[m]['substantive_delivery']['rate'])})" for m in MODES]],
*[[status,*[summaries[m]["status_counts"].get(status,0) for m in MODES]] for status in ("answered","partial","insufficient","failed","unknown")],
["全部必要证据被引用题数",*[sum(v["citation_all_necessary"]==1 for v in per[m].values()) for m in MODES]],
["引用必要证据覆盖率",*[pct(summaries[m]["rank_and_coverage"]["citation_coverage"]["observed_mean"]) for m in MODES]],
["带citation_id的claim比例",*[pct(summaries[m]["claim_citation_id_rate"]) for m in MODES]],
["有修订的请求数（已观测）",*[summaries[m]["requests_with_revision"] for m in MODES]],
["契约错误请求",*[summaries[m]["original_contract_counts"]["contract_error_requests"] for m in MODES]],
["契约空答案",*[summaries[m]["original_contract_counts"]["contract_empty_answers"] for m in MODES]]]),
"服务成功包含insufficient，不代表可用回答。带引用ID不代表引用真实支持论断。unknown终局在交付统计中仅表示未观测到交付，不证明实际没有答复。",
"## 延迟（秒）",
table(["样本及分位","A0","F","B4"],[[f"{label} {k}",*[str(summaries[m][field]["n"]) if k=="n" else f"{summaries[m][field][k]/1000:.3f}" for m in MODES]] for field,label in (("latency_ms_all_observed","全部已测"),("latency_ms_service_success","正常返回"),("latency_ms_substantive_delivery","实质交付")) for k in ("n","mean","p50","p90","p95","p99","min","max")]),
"分位数采用nearest-rank。快速失败会降低整体P50；n≤40时P99基本就是最大值，尾延迟估计不稳定。阶段耗时及检索依赖耗时见metrics.json：是下游调用耗时之和，不是完整RAG阶段wall-clock，也没有TTFT或压测吞吐。",
"## 检索依赖与模型阶段调用耗时（秒）",
"以下是每请求已记录的物理调用耗时之和，不含全部本地处理时间，不能当成完整阶段wall-clock。检索依赖合计含retrieval、embedding、reranking及实验模式navigation；仅纳入已存在上下文快照且有journal的请求，各模式33题。各阶段单列则按该阶段有记录的请求取样，样本数不同，分位数不可相加。",
table(["检索依赖合计","A0","F","B4"],[[k,*[str(summaries[m]["retrieval_dependency_call_sum_ms"]["n"]) if k=="n" else f"{summaries[m]['retrieval_dependency_call_sum_ms'][k]/1000:.3f}" for m in MODES]] for k in ("n","mean","p50","p90","p95","p99")]),
table(["模式","阶段","n","P50","P90","P95"],[[m,stage,data["n"],*[f"{data[k]/1000:.3f}" for k in ("p50","p90","p95")]] for m in MODES for stage,data in summaries[m]["stage_observed_call_sum_ms"].items()]),
"## 更多检索指标（共同31题）",
table(["指标","A0","F","B4"],[[f"{pre} {kind}@{k}",*[pct(paired[m][f"{pre}_{field}_{k}"]) for m in MODES]] for pre in ("candidate","reranked") for kind,field in (("Hit","hit_at"),("Precision","precision_at"),("全部gold命中","all_gold_at")) for k in (3,5,10)]),
"Hit@K为前K条并集至少完整覆盖一段gold的题比例；全部gold命中为覆盖全部gold的题比例；Precision@K为单叶完整覆盖至少一段gold的候选数/K。它们衡量原文坐标，不是语义相关性或答案正确率。",
"## 资源与契约",
table(["指标","A0","F","B4"],[[k,*[summaries[m]["original_usage_accounting"].get(k) for m in MODES]] for k in ("embedding_calls","rerank_calls","chat_calls","observed_input_tokens","observed_output_tokens","unknown_usage_calls","unknown_journals","complete")]),
"Token仅为已观测小计，包含导航/生成/核验等已有记账；usage不完整且未冻结价格，费用总计unknown，不推算每题金额。",
"## A0四十题独立AI语义复核",
"本次由当前AI逐题对照问题、金标准引文及实际答案。非人工、非盲评、非多评审一致性检查；风险标记包含错误和需复核推断，不能作为已确认幻觉率。",
table(["指标","数量/分母","比例"],[
["明确覆盖提问核心子问",f"{len(complete)}/40",pct(len(complete)/40)],
["核心完整且本次未见明显风险",f"{passed}/40",pct(passed/40)],
["上述结果在实质答复中",f"{passed}/29",pct(passed/29)],
["部分回答","4/40",pct(4/40)],["无法给出实质回答","4/40",pct(4/40)],["调用失败","6/40",pct(6/40)],["结果未知","1/40",pct(1/40)],
["需复核错误/过度推断",f"{len(flags)}/29",pct(len(flags)/29)]]),
"上述21/40不是人工确认准确率；仅为本次单AI的探索性严格可接受率。它不证明剩余答复全错。第21、27题虽然流程partial，正文已满足问题；第9、15、33、37题流程answered，但有错误或推断风险。",
table(["领域","题数","核心完整","完整且未见明显风险"],[[d,v["n"],v["question_complete"],v["acceptable_complete"]] for d,v in reviewmeta["by_domain"].items()]),
"## 逐题复核",
table(["题号","领域","流程状态","提问完成度","风险","说明"],[[x["question_id"],x["domain"],per["a0"][x["question_id"]]["status"],x["question_completion"],"是" if x["risk_flag"] else "否",x["note"].replace("|","/")] for x in review]),
"## 指标选用建议与缺失项",
"优先选：候选与重排Recall@3/5/10（注明n和并集口径）、MRR、上下文覆盖、引用覆盖、全40题实质交付率、AI语义完整可接受率（明确AI复核）、P50/P90/P95以及请求失败率。保留NDCG作历史比较时必须说明自定义口径；不建议将它包装成标准NDCG。",
"可选：Hit@K、Precision@K（坐标口径）、全gold命中率、Recall@1/20/40、均值/最大/P99延迟、成功子集和交付子集延迟、分领域表现、上下文与引用全覆盖题数、修订率、契约错误率、引用ID覆盖率、调用数、已观测token。以上都在metrics.json。",
"不能据现有样本给出：人工正确率/幻觉率、可靠RAGAS faithfulness、无答案题拒答precision/recall/F1（40题全可答）、标准候选级NDCG（缺完整候选相关性标注）、TTFT、真实并发QPS、稳定性方差（每题一次）、完整金额。补测无法追溯修复旧实验未知耗时/账单；相应项目需要新增观测或评审任务。",
"金标准争议：第10、26题有题目未要求的附加评分点；第20题dot锚点与证据不匹配；第24题代码围栏锚点含义不清。所有金标准仍pending，现有分数只宜作为开发诊断，不宜直接用作论文或产品准确率承诺。",
"原始数据目录："+str(SRC),
"复算：使用Python 3执行同目录audit.py，仅读取冻结原始记录并更新本目录派生产物；不会调用模型、修改数据库或覆盖原实验。"]
(OUT/"report.md").write_text("\n\n".join(report)+"\n",encoding="utf-8")
answers=["# 原始40题×3方案答卷摘录","历史原始内容，不是本次生成。每题给出金标准引文、标注答案要点和三方案最终答复；引用完整坐标仍以源dev.jsonl为准。"]
for q in questions:
    answers.extend(["## "+q["question_id"],q["question"],"金标准要点：\n"+"\n".join("- "+p["text"] for p in q["required_answer_points"]),"金标准证据：\n"+"\n".join(g["quote"] for g in q["necessary_evidence"])])
    for m in MODES:
        r=next(r for r in records if r["question_id"]==q["question_id"] and r["plugin"]==m)
        answers.extend(["### "+m.upper()+" · "+str(r.get("answer_status") or r.get("error_code")), (r.get("response") or {}).get("text") or "无已记录答复。"])
(OUT/"answers.md").write_text("\n\n".join(answers)+"\n",encoding="utf-8")
print(json.dumps({"checks":meta["checks"],"common_n":len(common),"review":reviewmeta["summary"],"files":[str(OUT/n) for n in ("report.md","metrics.json","ai-review-a0.json","answers.md")]},ensure_ascii=False))
