"""Readable projections of generated note facts; never mutate saved user text."""
import re

VERDICTS = {'correct': '正确', 'incorrect': '错误', 'partial': '部分正确', 'unverified': '未验证'}


def plain_summary(value, limit=120):
    text = re.sub(r'```[^\n]*\n?|```|[*`#]', '', str(value or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def question_summaries(questions):
    return [{'id': q['id'], 'number': index + 1, 'summary': plain_summary(q.get('prompt')) or '题目内容未记录'}
            for index, q in enumerate(questions)]


def source_label(ref):
    name = ref.get('material_name') or '原始资料'
    section = ' / '.join(str(s) for s in ref.get('section_path', []) if s)
    location = f"第 {ref['page']} 页" if ref.get('page') else ''
    if not location and ref.get('line_start'):
        start, end = ref['line_start'], ref.get('line_end')
        location = f'第 {start}—{end} 行' if end and end != start else f'第 {start} 行'
    return ' · '.join(str(x) for x in (name, section, location or '位置未记录') if x)


def unique_sources(refs):
    seen, result = set(), []
    for ref in refs:
        key = tuple(ref.get(k) for k in ('material_id', 'material_version_id', 'chunk_id'))
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def readable_markdown(markdown, facts, questions, refs, assessment_id):
    """Translate only exact legacy machine-generated lines in known sections."""
    summaries = {q['id']: q['summary'] for q in question_summaries(questions)}
    results = facts.get('question_results', [])
    labels = {q['question_id']: f"第 {i + 1} 题：{summaries.get(q['question_id'], '题目内容未记录')}" for i, q in enumerate(results)}
    result_map = {q['question_id']: q for q in results}
    sources = {r['chunk_id']: source_label(r) for r in unique_sources(refs) if r.get('chunk_id')}

    def section(match):
        heading, body = match.group(1), match.group(2)
        output, seen_sources = [], set()
        for line in body.splitlines(keepends=True):
            content, newline = line.rstrip('\r\n'), '\n' if line.endswith('\n') else ''
            if heading.startswith('## 测评结果'):
                qid = content.removeprefix('- ').split('：', 1)[0]
                q = result_map.get(qid)
                if q and re.fullmatch(re.escape('- ' + qid) + r'：\w+；分数 .+', content):
                    score = q.get('score')
                    content = f"- {labels[qid]} — {VERDICTS.get(q.get('verdict'), '未验证')}；得分 {score if score is not None else '未验证'}" + ('；有辅助记录' if q.get('assisted') else '')
            elif heading.startswith('## 理解纠偏'):
                prefix = '- 纠偏记录：' + assessment_id + ':'
                qid = content.removeprefix(prefix)
                if content.startswith(prefix) and qid in labels:
                    content = f'- {labels[qid]} — 已记录作答纠正，说明见本章纠偏内容'
            elif heading.startswith('## 待巩固内容'):
                qid = content.removeprefix('- ')
                if content.startswith('- ') and qid in labels:
                    content = f'- {labels[qid]} — 需要巩固'
            elif heading.startswith('## 来源'):
                link = re.fullmatch(r'- \[([^\]]+)\]\(/api/v1/materials/[^\s)]+\)', content)
                if link and link[1] in sources:
                    if link[1] in seen_sources:
                        continue
                    seen_sources.add(link[1])
                    content = '- ' + sources[link[1]]
            output.append(content + newline)
        return heading + ''.join(output)

    return re.sub(r'(^## (?:测评结果|理解纠偏|待巩固内容|来源)\n)(.*?)(?=^## |\Z)', section, markdown, flags=re.M | re.S)
