"""Pure Chinese Markdown rendering of frozen B4 analysis; no inference of missing data."""
from html import escape
import json
import math

MODES = ('a0', 'f', 'b4')
KS = (1, 3, 5, 10, 20, 40)


def _text(value):
    if value is None:
        return 'unknown'
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return escape(str(value), quote=False).replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def _number(value, digits=0, *, percent=False, pp=False):
    if type(value) not in (float, int) or not math.isfinite(value):
        return 'unknown'
    if percent:
        return f'{value * 100:.2f}%'
    if pp:
        return f'{value * 100:+.2f} pp'
    return f'{value:.{digits}f}'


def _status(value):
    return '通过' if value is True else '未通过' if value is False else 'unknown'


def _get(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |',
                      *['| ' + ' | '.join(row) + ' |' for row in rows]])


def render_markdown(analysis):
    """Return a serializable report's Markdown, without changing its values.

    Missing means stay unknown; observed subtotals and observation counts are
    labeled separately. Threshold deltas are question-macro; family-equal
    paired estimates and intervals are reported in a separate table.
    """
    if not isinstance(analysis, dict):
        raise ValueError('analysis must be an object')
    modes = analysis.get('modes') or {}
    sections = ['# B4 最终树形检索实验报告']
    decisions = {'unknown': 'unknown：证据未齐，暂不能作保留或退出结论',
                 'drop_online_tree': '不保留在线树检索；不继续追加 B5',
                 'retain_controlled_optional': '满足工程门槛，可作为受控可选模式保留'}
    sections.append('结论：' + decisions.get(analysis.get('decision'), 'unknown') + '。默认模式：' +
                    _text(analysis.get('default_mode')).upper() + '。')
    n, repeats = analysis.get('independent_questions'), analysis.get('repeats')
    sections.append(f"调度：{_number(n)} × 3 × {_number(repeats)} = {_number(analysis.get('scheduled_requests'))} 次端到端请求；"
                    f"实际终局记录 {_number(analysis.get('actual_requests'))} 条。A0/F/B4 逐题轮换，每题每模式仅执行一次。"
                    '缺失和失败保留在预定分母；未知项不按零填充。')
    validity = analysis.get('experiment_validity') or {}
    sections.append('实验有效性：' + _text(validity.get('status')) + '；原因：' + _text(validity.get('reason')) + '。')

    def mode_table(title, fields):
        sections.append('## ' + title)
        rows = []
        for label, path, fmt in fields:
            rows.append([label, *[fmt(_get(modes.get(mode), *path)) for mode in MODES]])
        sections.append(_table(['指标', 'A0', 'F', 'B4'], rows))

    mode_table('执行进度与独立分母', [(label, (field,), _number) for label, field in (
        ('预定请求数', 'scheduled'), ('已记录尝试', 'attempted'), ('已完成终局', 'completed'),
        ('请求级失败', 'failed'), ('已发出但结果未知', 'unknown'), ('尚无记录', 'missing'))])
    coverage = [('生成前必要证据覆盖率', 'context_coverage'), ('最终引用必要证据覆盖率', 'citation_coverage')]
    mode_table('必要证据覆盖与交付', [(label, ('metrics', name, 'mean'), lambda x: _number(x, percent=True))
        for label, name in coverage] + [(label, (field,), _number) for label, field in (
        ('非空已核验交付数', 'nonempty_verified_delivery'), ('完整回答数', 'full_answer_count'))] + [
        ('完整回答率', ('complete_answer_rate',), lambda x: _number(x, percent=True)),
        *[(label + '：全部必要证据齐全题数', ('all_necessary_counts', prefix), _number)
          for label, prefix in (('候选', 'candidate'), ('重排', 'reranked'), ('上下文', 'context'), ('引用', 'citation'))]])
    sections.append('上下文来自生成前独立快照；生成失败不会把已有上下文计零。最终引用失败或空答案按零计分。'
                    '“完整回答”是流程状态，必要证据坐标覆盖不等于事实正确性。')
    rows = []
    for label, metric in coverage:
        b4 = _get(modes.get('b4'), 'metrics', metric, 'mean')
        for base in ('a0', 'f'):
            value = _get(modes.get(base), 'metrics', metric, 'mean')
            delta = b4 - value if type(b4) in (float, int) and type(value) in (float, int) else None
            rows.append([label, 'B4−' + base.upper(), _number(delta, pp=True)])
    sections.append(_table(['门槛指标', '比较', '问题等权差值'], rows))
    mode_table('覆盖观测完整性', [(label + '已测/未知/预定', ('metrics', name),
        lambda x: '/'.join(_number(_get(x, k)) for k in ('measured', 'unknown', 'scheduled')))
        for label, name in coverage])

    for prefix, name in (('candidate', '候选'), ('reranked', '重排')):
        for kind, label in (('legacy', '原单叶口径'), ('union', '坐标并集口径')):
            mode_table(name + '：' + label, [(f'Recall@{k}', ('metrics', f'{prefix}_{kind}_recall_at_{k}', 'mean'),
                         lambda x: _number(x, percent=True)) for k in KS])
    mode_table('原口径排序指标', [(name + ' ' + label, ('metrics', f'{prefix}_legacy_{metric}', 'mean'),
                 lambda x: _number(x, 4)) for prefix, name in (('candidate', '候选'), ('reranked', '重排'))
                 for metric, label in (('ndcg_at_10', 'NDCG@10'), ('mrr', 'MRR'))])
    sections.append('重排指标按统一叶子展开计算；坐标并集 Recall 与原单叶口径分别展示，未创造并集 NDCG。')
    mode_table('交付状态与错误', [(status, ('status_counts', status), _number)
        for status in ('answered', 'partial', 'insufficient', 'clarify', 'failed', 'unknown', 'missing')] + [
        (label, (field,), _number) for label, field in (
        ('契约错误请求', 'contract_error_requests'), ('契约空答案', 'contract_empty_answers'),
        ('恢复的部分回答', 'recovered_partial'), ('provider 失败请求', 'provider_failure_requests'),
        ('越权或无来源引用', 'source_violations'))])
    sections.append(_table(['模式', '已记录错误类别'], [[mode.upper(), _text(_get(modes.get(mode), 'error_categories'))]
                                                for mode in MODES]))

    mode_table('在线调用、token 与时延', [
        *[(label, ('usage', field), _number) for label, field in (
        ('embedding 调用数', 'embedding_calls'), ('rerank 调用数', 'rerank_calls'), ('chat 调用数（含导航）', 'chat_calls'),
        ('已观察输入 token', 'observed_input_tokens'), ('已观察输出 token', 'observed_output_tokens'),
        ('usage 未知调用数', 'unknown_usage_calls'), ('未知 journal 数', 'unknown_journals'),
        ('导航账目不一致数', 'navigation_disagreements'))],
        ('平均输入 token', ('usage', 'mean_input_tokens'), lambda x: _number(x, 2)),
        ('平均输出 token', ('usage', 'mean_output_tokens'), lambda x: _number(x, 2)),
        ('调用账目完整', ('usage', 'complete'), _status),
        *[(f'总时延 {q.upper()}（ms）', ('latency_ms', q), lambda x: _number(x, 2)) for q in ('p50', 'p95')],
        ('总时延有效观测数', ('latency_ms', 'measured'), _number),
        ('总时延观测完整', ('latency_ms', 'complete'), _status),
        ('导航实际调用数', ('navigation', 'calls'), _number),
        ('导航调用上限检查', ('navigation', 'call_budget_ok'), _status),
        *[(f'纯导航时延 {q.upper()}（ms）', ('navigation', 'latency_ms', q), lambda x: _number(x, 2)) for q in ('p50', 'p95')],
        ('导航时延有效观测数', ('navigation', 'latency_ms', 'measured'), _number),
        ('导航失败请求', ('navigation', 'failure_requests'), _number),
        ('导航回退请求', ('navigation', 'fallback_requests'), _number)])
    mode_table('在线金额', [('在线金额总计（单位见冻结定价）', ('cost', 'total'), lambda x: _number(x, 6)),
        ('已观察金额小计（非总计）', ('cost', 'observed_subtotal'), lambda x: _number(x, 6)),
        ('金额未知请求数', ('cost', 'unknown'), _number)])
    sections.append('回退及失败调用的成本与耗时保留；价格或 usage 不完整时金额为 unknown，不能称经济门槛已验证。'
                    '金额状态：' + _text(analysis.get('monetary_gate')) + '。')
    sections.append('## 索引构建资源')
    sections.append('F 与 B4 共享预处理成本，摘要与 embedding 单独记账；以下仅展示已提供的建库记录，缺失项保持 unknown。')
    build = analysis.get('index_build')
    accounting = build.get('accounting_complete') if isinstance(build, dict) else None
    sections.append('索引账目完整性：' + ('完整' if accounting is True else '不完整' if accounting is False else 'unknown') + '。')
    unresolved = build.get('prior_unresolved_summary_attempts') if isinstance(build, dict) else None
    if unresolved:
        sections.append('历史未解决摘要尝试：' + _text(unresolved) + '。历史 unknown 调用可能已计费，'
                        '不能因后续重试成功而清除；其费用、usage 和结果仍须保持未知。')
    sections.append(_table(['记录字段', '值'], [[_text(k), _text(v)] for k, v in build.items() if k != 'usage']
                          if isinstance(build, dict) and build else [['索引构建记录', 'unknown']]))
    index_usage = build.get('usage') if isinstance(build, dict) else None
    if isinstance(index_usage, list):
        rows = []
        for call in index_usage:
            if not isinstance(call, dict):
                rows.append(['unknown'] * 7)
                continue
            usage = call.get('usage') or {}
            rows.append([_text(call.get('kind')), _text(call.get('call_id')), _text(call.get('model')),
                '是' if call.get('cached') is True else '否' if call.get('cached') is False else 'unknown',
                _number(usage.get('input_tokens', usage.get('prompt_tokens', usage.get('total_tokens')))),
                _number(usage.get('output_tokens', usage.get('completion_tokens'))),
                _number(call.get('elapsed_seconds'), 2)])
        sections.append(_table(['类型', '调用 ID', '模型', '缓存', '输入 token', '输出 token', '耗时（秒）'], rows))
        sections.append('缓存命中仍保留原始调用资源；不将缓存记录再次计费，也不把缺失 usage 写成零。')
    elif index_usage is not None:
        sections.append(_table(['索引 usage', '记录'], [['原始计数', _text(index_usage)]]))

    sections.append('## 配对差异与不确定性')
    sections.append(f"family 等权，{_number(analysis.get('independent_families'))} 个 family，"
                    f"bootstrap {_number(analysis.get('bootstrap_samples'))} 次，seed {_number(analysis.get('seed'))}，95% 区间。"
                    '这里的 family 等权差异与上面的问题等权门槛差值分开报告。')
    rows = []
    for pair in ('b4-a0', 'b4-f'):
        for metric, value in (analysis.get('comparisons', {}).get(pair) or {}).items():
            ci = value.get('ci95')
            interval = '[' + ', '.join(_number(v, pp=True) for v in ci) + ']' if isinstance(ci, list) and len(ci) == 2 else 'unknown'
            rows.append([pair.upper(), _text(metric), _number(value.get('difference'), pp=True), interval])
    sections.append(_table(['比较', '指标', 'family 等权差', '95% 区间'], rows))
    sections.append('## 上下文变化与来源归因')
    rows = []
    for pair in ('b4-a0', 'b4-f'):
        attribution = (analysis.get('attribution') or {}).get(pair) or {}
        for key, label in (('changed_context', '上下文改变'), ('same_context', '上下文相同'), ('unknown_context', '上下文 hash 未知')):
            group = attribution.get(key) or {}
            rows.append([pair.upper(), label, _number(group.get('count')), _number(group.get('citation_contribution'), pp=True),
                         _text(group.get('question_ids'))])
    sections.append(_table(['比较', '题组', '题数', '对全体引用差的贡献', '题号'], rows))
    sections.append('相同输入下的引用差异不能直接归功于树。新增金标准证据表示候选覆盖潜力，不能直接视为有效回答收益。')
    attribution = _get(analysis, 'attribution', 'b4-a0', 'per_question') or {}
    sections.append(_table(['题号', '新增 gold 索引', 'A 前20保留', '候选叶数', '上下文叶数', '路由遗漏', '包溢出'], [
        [_text(qid), _text(row.get('added_gold_evidence_indexes')), _status(row.get('a_top20_retained')),
         _number(row.get('candidate_leaf_count')), _number(row.get('context_leaf_count')),
         _text(row.get('route_omissions')), _text(row.get('packet_overflow'))] for qid, row in attribution.items()]))
    sections.append('冻结负标签题：' + _text(analysis.get('negative_label_question_ids')) + '。03/10 结构审计单列、不改标签：' +
                    _text(analysis.get('structural_audit_question_ids')) + '。')
    sections.append(_table(['冻结负标签上下文覆盖', 'A0', 'F', 'B4'], [['均值', *[
        _number(_get(analysis, 'negative_label_metrics', mode, 'mean'), percent=True) for mode in MODES]]]))

    sections.append('## 保留门槛逐项裁决')
    labels = {'context_coverage': ('实际原文进入上下文', '相对 A0、F 分别至少 +3pp'),
        'citation_coverage': ('最终引用', '相对 A0、F 分别至少 +5pp，含失败/空答案'),
        'context_attribution': ('收益归因', '变化输入题贡献为正，且至少两题通过盲审确认真实上下文收益'),
        'ranking_and_local_tasks': ('排序与局部任务', 'NDCG@10 及原负标签上下文下降不超过 1pp，无新增严重错误'),
        'delivery_reliability': ('交付与可靠性', '非空交付不低于两组；契约空答、请求失败不高于两组；来源违规为零'),
        'latency': ('时延', '总 P95 ≤ A0 × 1.2，不剔除慢请求或回退'),
        'online_cost_proxy': ('在线成本代理', '平均输入/输出 token 分别 ≤ A0 × 1.5；F/B4 导航每请求 ≤2次'),
        'quality_review': ('质量复核', '所有分数变化/新错误按原文盲审，不以不可靠内容换取覆盖')}
    sections.append(_table(['门槛', '预先冻结要求', '结果'], [[*labels.get(key, (_text(key), _text(gate.get('requirement')))),
        _status(gate.get('passed'))] for key, gate in (analysis.get('gates') or {}).items()]))
    review = analysis.get('quality_review') or {}
    reviewer = {'human': '人工复核', 'automated': '自动辅助评审', 'model': '自动辅助评审'}.get(review.get('reviewer_type'), 'unknown')
    sections.append('## 质量审查与结论边界')
    sections.append('审查状态：' + _text(review.get('status')) + '；审查类型：' + reviewer + '；正确性：' +
                    ('通过审查' if review.get('correctness') is True else '未通过审查' if review.get('correctness') is False else 'unknown') +
                    '。自动评分不等于人工复核；未完成按原文、隐藏模式标签的质量审查时不得上线。')
    sections.append('待审范围：' + _text(review.get('required_question_ids')) + '；已审题号：' + _text(review.get('reviewed_question_ids')) + '。')
    sections.append('本轮使用已见过的开发题，单次结果仅支持项目工程取舍，不能声称盲测、泛化结论、同题生成方差估计或稳定统计提升。'
                    'CI 包含零等不确定性按原值保留；不选择性补跑，不追加 B5。')
    sections.append('分析版本：' + _text(analysis.get('version')) + '；输入摘要：' + _text(analysis.get('input_sha256')) + '。')
    return '\n\n'.join(sections) + '\n'
