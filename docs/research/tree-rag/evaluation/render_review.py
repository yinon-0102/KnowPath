"""Render a frozen run for human review, without changing or scoring its labels."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'py'))
from knowpath_backend.rag_eval.dataset import verify_freeze
from knowpath_backend.rag_eval.scoring import report, evidence_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    frozen, dataset, split = verify_freeze(args.freeze)
    summary = report(args.freeze, args.results)
    records = [json.loads(line) for line in Path(args.results).read_text(encoding='utf-8').splitlines() if line.strip()]
    indexed = {(r['question_id'], r['plugin'], r['repeat']): r for r in records}
    selected = [q for q in dataset if q['question_id'] in split[summary['partition']]['question_ids']]
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    lines = ['# 开发集逐题复核材料', '', f"冻结 ID：`{frozen['freeze_id']}`。", '',
        '以下金标是待人工审核草稿；模型 answered/supported 不代表人工判定正确。'
        '本文件不填写胜负或通过结论。证据覆盖是原文区间覆盖，不是语义正确率。', '',
        '请逐题核对：需求是否合理、关键结论是否正确且有引用支持、条件/例外是否完整、'
        '是否误拒或无依据回答、错误发生在检索/上下文/生成/核验还是服务阶段。', '',
        '填写模板时只将已完成复核的条目复制到 human-reviews.json；success 与 partial 须为布尔值，'
        'error_type 填主要错误类型，无错误填空字符串。不要把 null 改成 false 来冒充已复核。', '']
    template = []
    def percent(value):
        return '未知' if value is None else f'{value:.1%}'
    for question in selected:
        qid = question['question_id']
        lines.extend([f"## {qid}：{question['question']}", '',
            f"资料：`{question['source_filename']}`；预期状态草稿：`{question['expected_answer_status']}`。", '',
            '答案要点草稿：', ''])
        lines.extend('- ' + point['text'] for point in question['required_answer_points'])
        for point in question.get('missing_answer_points', []):
            lines.append('- 缺失项：' + (point if isinstance(point, str) else json.dumps(point, ensure_ascii=False)))
        lines.extend(['', '<details><summary>必要证据草稿（需人工核对）</summary>', ''])
        for span in question.get('necessary_evidence', []):
            lines.extend([f"原文页 {span.get('page')}，块 `{span['block']}`，"
                f"区间 [{span['start']}, {span['end']})：", ''])
            lines.extend(('> ' + line if line else '>') for line in span.get('quote', '').splitlines())
            lines.append('')
        lines.extend(['</details>', '', '| 组别 | 状态/错误 | 秒 | 候选覆盖 | 重排覆盖 | 上下文覆盖 | 引用覆盖 |',
            '|---|---|---:|---:|---:|---:|---:|'])
        for repeat in range(frozen['config']['repeats']):
            for mode in ('a', 'b1'):
                record = indexed.get((qid, mode, repeat))
                response = record.get('response') if record else None
                values = [percent(evidence_coverage(response, question, field))
                    for field in ('candidate_sources', 'reranked_sources', 'sources', 'citations')]
                state = (record['answer_status'] or record['error_code']) if record else '未执行'
                latency = f"{record['latency_ms']/1000:.1f}" if record else '未知'
                lines.append(f"| {mode.upper()} / {repeat} | {state} | {latency} | " + ' | '.join(values) + ' |')
                template.append(dict(question_id=qid, plugin=mode, repeat=repeat,
                    success=None, partial=None, error_type=None, reviewer=None, notes=''))
        for repeat in range(frozen['config']['repeats']):
            for mode in ('a', 'b1'):
                record = indexed.get((qid, mode, repeat))
                response = record.get('response') if record else None
                lines.extend(['', f'### {mode.upper()} / {repeat} 回答', ''])
                if not response:
                    lines.append('无可发布回答；错误见上表。')
                    if record and record.get('error_details'):
                        lines.extend(['', '安全诊断：`' + json.dumps(record['error_details'], ensure_ascii=False) + '`。'])
                    continue
                lines.extend(('> ' + line if line else '>') for line in response['text'].splitlines())
                lines.extend(['', '引用定位：', ''])
                for citation in response.get('citations', []):
                    for span in citation['source_spans']:
                        lines.append(f"- `{citation['chunk_id']}`：页 {span.get('page')}，"
                            f"原文块 `{span['block']}`，区间 [{span['start']}, {span['end']})。")
                lines.extend(['', '<details><summary>本次模型可见的完整证据</summary>', ''])
                for source in response.get('sources', []):
                    lines.extend([f"Chunk `{source['chunk_id']}`：", ''])
                    lines.extend(('> ' + line if line else '>') for line in source['source_text'].splitlines())
                    lines.append('')
                lines.extend(['</details>', ''])
        lines.extend(['', '人工复核：待填写。', ''])
    (destination/'review.md').write_text('\n'.join(lines), encoding='utf-8')
    (destination/'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    overview = ['# 真实开发集运行报告', '', f"冻结 ID：`{frozen['freeze_id']}`。", '',
        f"已执行 {len(records)} 个请求；未执行计划请求 {summary['missing_execution_rows']} 个。"
        '每题 A/B1 配对，交替顺序，无隐式重试。', '',
        '标签尚待人工审核；answered、partial 等为运行时状态，不能作为人工任务成功率。'
        '价格与部署门槛尚未确定，费用和采用结论保持未知。', '',
        '| 指标 | A | B1 |', '|---|---:|---:|']
    for title, key in [('计划请求', 'denominator'), ('正常返回状态', 'service_success_denominator'),
                       ('服务失败', 'service_failures'), ('未执行', 'missing_execution_rows'),
                       ('待人工判分', 'human_review_missing')]:
        overview.append(f"| {title} | {summary['plugins']['a'][key]} | {summary['plugins']['b1'][key]} |")
    for title, key in [('P50 延迟（秒）', 'p50_latency_ms'), ('P95 延迟（秒）', 'p95_latency_ms')]:
        values = [summary['plugins'][mode][key] for mode in ('a', 'b1')]
        overview.append(f'| {title} | ' + ' | '.join('未知' if value is None else f'{value/1000:.2f}'
            for value in values) + ' |')
    overview.extend(['', '延迟包含失败请求；未执行请求没有测得延迟。两组各一次，不能据此推断稳定性能。', '',
        '## 运行时状态与错误', '', '| 状态/错误 | A | B1 |', '|---|---:|---:|'])
    states = {mode: Counter((record['answer_status'] if record['service_success'] else record['error_code'])
        or 'unknown' for record in records if record['plugin'] == mode) for mode in ('a', 'b1')}
    for state in sorted(set(states['a']) | set(states['b1'])):
        overview.append(f"| {state} | {states['a'][state]} | {states['b1'][state]} |")
    overview.extend(['', '## 证据覆盖', '',
        '下面按待审核金标区间计算几何覆盖率，仅统计有返回证据的请求。服务失败仍计入上方完整分母，'
        '其证据不可观测，不以零或成功代替。覆盖率不能说明语义正确或条件完整。', '',
        '| 阶段 | A 覆盖率（可观测请求） | B1 覆盖率（可观测请求） |', '|---|---:|---:|'])
    for title, prefix in [('候选', 'candidate'), ('重排', 'rerank'), ('上下文', 'context'), ('引用', 'citation')]:
        values = [f"{percent(summary['plugins'][mode][prefix+'_evidence_coverage'])}"
                  f"（{summary['plugins'][mode][prefix+'_coverage_denominator']}）" for mode in ('a', 'b1')]
        overview.append(f'| {title} | ' + ' | '.join(values) + ' |')
    overview.extend(['', '## 后续验收', '',
        '先复核 [逐题回答与原文](review.md)，填写已完成复核的人工评分；确认价格、费用及延迟门槛后，'
        '再冻结保留集实验。当前报告不支持采用 B1 或上线任一新配置的结论。', '',
        '详细机器统计见 [report.json](report.json)。原始失败不得删除，后续配置改动必须另建实验。', ''])
    (destination/'summary.md').write_text('\n'.join(overview), encoding='utf-8')
    draft = destination/'human-review-template.json'
    if not draft.exists():
        draft.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'questions': len(selected), 'review_entries': len(template)}))


if __name__ == '__main__':
    main()
