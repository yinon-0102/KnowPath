import { esc } from './store.js';

const rows = value => Array.isArray(value) ? value : [];
const eventTime = value => {
  if (typeof value !== 'string' || !value.trim()) return '时间未记录';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间无效，请刷新核对';
  return esc(new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }).format(date)) + '（本地时间）';
};
export const assessmentKindLabel = kind => ({ diagnostic: '自适应诊断', practice: '专项练习', retest: '独立复测' })[kind] || '学习测评';

export function renderAssistanceReasons(events) {
  const names = { request_hint: '请求提示', source_read: '查阅原文', question_feedback: '查看同源题目解析', message: '学习助手帮助',
    note_read: '阅读学习笔记', note_edit: '编辑学习笔记', lesson_read: '阅读个性化讲解', lesson_stream: '接收个性化讲解' };
  return rows(events).map(event => `<p class="subtle">辅助原因：${esc(names[event.kind] || '学习帮助（' + event.kind + '）')} · ${eventTime(event.delivered_at)}</p>`).join('');
}

export function renderResultSummary(result) {
  const questions = rows(result?.question_results), summary = result?.summary || {};
  const count = key => Number.isInteger(summary[key]) ? summary[key] : questions.filter(q => q.verdict === key).length;
  return `<p class="notice">正确 ${count('correct')} 题 · 错误 ${count('incorrect')} 题 · 待核验 ${count('unverified')} 题 · 本轮当前有效独立证据 ${summary.independent_evidence_count ?? '未记录'} 条。</p><p class="subtle">客观题按封存答案判分。简答保存作答和参考反馈，当前不自动可靠判分；待核验暂不计自动得分，不计为错误或掌握证据。辅助与重复题族也不增加有效独立证据。</p>`;
}

export function renderMasteryGaps(items, topics = []) {
  return rows(items).map(item => {
    const coverage = item.evidence_sufficiency;
    if (!coverage) return '';
    const requirements = coverage.requirements || {}, missing = rows(coverage.missing);
    const title = rows(topics).find(topic => topic.id === item.topic_id);
    const explanations = {
      insufficient_independent_families: `需要 ${requirements.minimum_families ?? '更多'} 个独立题族，当前 ${coverage.independent_families ?? 0} 个`,
      insufficient_distinct_assessments: `需要 ${requirements.minimum_assessments ?? '更多'} 轮不同测评，当前 ${coverage.distinct_assessments ?? 0} 轮`,
      insufficient_observation_spacing: `独立观察需相隔 ${requirements.minimum_spacing_hours ?? '规定'} 小时，当前 ${Math.round((coverage.spacing_hours || 0) * 10) / 10} 小时`,
      no_application_observation: '缺少独立应用题观察',
    };
    return `<section class="learning-topic"><h3>${esc(title?.name || title?.title || '知识主题')} · 掌握条件</h3>${missing.length ? `<ul>${missing.map(code => `<li>${esc(explanations[code] || code)}</li>`).join('')}</ul>` : '<p>当前证据覆盖已充分；掌握状态仍取决于正确率与有效性。</p>'}${coverage.earliest_retest_at && !coverage.can_retest_now ? `<p>尚未到间隔补测时间：${eventTime(coverage.earliest_retest_at)}。等待本身不会补足独立观察。</p>` : coverage.can_retest_now && missing.length ? `<p>现在可补充独立证据；请使用新题族。</p><button type="button" class="button button-secondary" data-action="review-topic" data-topic="${esc(item.topic_id)}">独立复测这个主题</button>` : ''}</section>`;
  }).join('');
}
