import { esc, dateText } from './store.js';
import { workspacePath, workspaceSection } from './workspace.js';
import { taskAssessmentContext } from './workbench.js';

const rows = value => Array.isArray(value) ? value : [];
const title = row => row?.name || row?.title || row?.section_path?.join(' / ') || '未命名知识项';
const link = (label, page, id, item, cls = 'text-link') => `<a class="${cls}" href="${workspacePath(page, ...[id, item].filter(x => x != null))}">${esc(label)}</a>`;
const action = (label, name, value = '', cls = 'button button-secondary') => `<button type="button" class="${cls}" data-workspace-action="${name}" data-value="${esc(value)}">${esc(label)}</button>`;
const check = (name, value, label, checked = false, extra = '') => `<label class="check-label"><input type="checkbox" name="${name}" value="${esc(value)}" ${checked ? 'checked' : ''} ${extra}><span>${label}</span></label>`;
const confirm = label => `<div class="workspace-confirm">${check('confirm', 'on', label, false, 'required')}</div>`;
const field = (label, name, value = '', attributes = '') => `<div class="form-field"><label for="ws-${name}">${label}</label><input id="ws-${name}" name="${name}" value="${esc(value)}" ${attributes}></div>`;
const area = (label, name, value = '', attributes = 'required maxlength="2000"') => `<div class="form-field"><label for="ws-${name}">${label}</label><textarea id="ws-${name}" name="${name}" rows="3" ${attributes}>${esc(value)}</textarea></div>`;
const options = (choices, selected = '') => choices.map(([id, label]) => `<option value="${esc(id)}" ${String(id) === String(selected) ? 'selected' : ''}>${esc(label)}</option>`).join('');
const select = (label, name, choices, selected = '', extra = '') => `<div class="form-field"><label for="ws-${name}">${label}</label><select id="ws-${name}" name="${name}" ${extra}>${options(choices, selected)}</select></div>`;
const empty = text => `<p class="workspace-empty">${esc(text)}</p>`;
const note = (text, kind = '') => `<p class="notice ${kind}">${text}</p>`;
const panel = content => `<section class="panel workspace-panel">${content}</section>`;
const form = (content, submit = '保存', danger = false) => `<form data-workspace-form><fieldset class="workspace-fields">${content}<div class="form-actions"><button class="button ${danger ? 'button-danger' : 'button-primary'}" type="submit">${submit}</button></div></fieldset></form>`;
const table = (head, content) => `<div class="workspace-table-wrap"><table class="workspace-table"><thead><tr>${head.map(label => `<th scope="col">${esc(label)}</th>`).join('')}</tr></thead><tbody>${content}</tbody></table></div>`;
const topicName = (data, id) => title(rows(data.topics || data.graph?.nodes).find(row => row.id === id)) === '未命名知识项' ? '历史主题（当前版本不可用）' : title(rows(data.topics || data.graph?.nodes).find(row => row.id === id));
const score = value => value == null ? '未验证' : `${Math.round(Number(value) * 100)}%`;
const labels = { unseen: '尚未学习', unstable: '掌握不稳定', needs_review: '需要复习', material_version: '资料新版本', graph_published: '图谱已发布', mastery_updated: '测评更新状态', plan_created: '创建计划', plan_replanned: '重新安排计划', knowledge_updated: '采用知识更新', state_reset: '学习状态重置', ready: '已就绪', uploaded: '待解析', processing: '处理中', pending: '待复核', pending_review: '待审核', indexing: '索引中', published: '已发布', superseded: '已有更新', failed: '失败', archived: '已归档', completed: '已完成', skipped: '已跳过', deferred: '已延期', active: '进行中', corrected: '已更正', invalid: '已作废', valid: '有效', unreviewed: '未复核', correct: '正确', incorrect: '错误', unverified: '未验证', unknown: '待学习', learning: '学习中', mastered: '已掌握', weak: '需巩固', review: '待复习', stale: '版本已变化', objective_answer: '客观题作答', short_answer: '简答题作答', grade_review: '评分复核', reset: '状态重置', report: '反馈题目', invalidate: '作废题目', reject: '驳回反馈', knowledge_update: '知识更新', knowledge_correction: '知识纠错', ok: '正常', degraded: '部分服务异常', unavailable: '不可用', configured: '已配置', not_configured: '未配置', eligible: '可用于评估' };
const label = value => esc(labels[value] || value || '暂无');
const sourceButtons = refs => rows(refs).map((ref, i) => action(`来源 ${i + 1}${ref.page ? ' · 第 ' + ref.page + ' 页' : ''}`, 'source', JSON.stringify(ref), 'text-link')).join('');
const reasonField = () => area('原因说明', 'reason', '', 'required maxlength="2000" placeholder="说明你发现的问题或希望调整的原因"');
const menu = choices => `<div class="workspace-menu">${choices.map(([name, description, page, id, item]) => { const href = page === 'graph' ? '#/materials/graph/' + encodeURIComponent(id) : workspacePath(page, ...[id, item].filter(x => x != null)); const attrs = page === 'graph' ? ` data-action="material-graph" data-id="${esc(id)}"` : ''; return `<a href="${href}"${attrs}><span><strong>${esc(name)}</strong><small>${esc(description)}</small></span><span aria-hidden="true">→</span></a>`; }).join('')}</div>`;
const versionChoice = data => select('资料版本', 'version_id', rows(data.versions).map(v => [v.id, `${v.filename} · ${dateText(v.created_at)} · ${labels[v.status] || v.status}`]), data.version?.id);

export const workspaceTitles = {
  material: '资料管理', 'material-name': '修改资料名称', 'material-archive': '归档资料', 'material-delete': '删除资料', 'material-versions': '资料版本', 'material-upload': '上传新版本', 'material-ingest': '重新解析资料', 'material-reconcile': '重新核对知识结构', 'material-graph': '审核知识图谱',
  'space-settings': '空间管理', 'space-name': '修改空间名称', 'space-profile': '学习目标与偏好', 'space-scope': '学习范围', 'knowledge-updates': '知识更新', 'knowledge-changes': '知识变更记录', 'knowledge-correct': '知识纠错', 'learning-state': '学习状态明细', 'learning-reset': '重置学习状态', evidence: '学习证据', export: '导出学习记录', health: '服务状态',
  'assessment-result': '测评详细结果', 'assessment-grade': '评分复核', 'assessment-report': '反馈题目问题', 'assessment-reviews': '题目复核记录', 'assessment-resolve': '处理题目反馈', task: '安排学习任务', session: '任务学习',
};
function materialView(page, id, data) {
  const material = data.material;
  if (page === 'material') return panel(`<div class="workspace-object"><span class="file-icon">文</span><div><h2>${esc(material.name)}</h2><p>${label(material.status)} · ${data.versions.length} 个版本 · ${data.affected.length} 个关联空间</p></div></div>` + menu([
    ['查看知识结构', '直接浏览这份资料的主题与关联', 'graph', id], ['资料版本', '查看历史版本与解析状态', 'material-versions', id], ['修改名称', '使用容易识别的教材或笔记名称', 'material-name', id],
    ['审核知识图谱', '核对新增、修改及冲突', 'material-graph', id], ['重新解析', '资料解析失败时重新处理', 'material-ingest', id],
    ['重新核对知识结构', '从已解析版本生成图谱候选', 'material-reconcile', id],
    ...(material.status !== 'archived' ? [['归档资料', '收起暂时不用的资料，保留内容', 'material-archive', id]] : []), ['删除资料', '查看关联影响后永久删除', 'material-delete', id],
  ]));
  if (page === 'material-name') return panel(form(field('资料名称', 'name', material.name, 'required maxlength="100"')));
  if (page === 'material-archive') return panel(material.status === 'archived' ? empty('这份资料已经归档。') : form(note('归档保留资料及已有学习记录。当前服务暂不支持恢复归档资料。') + confirm('确认归档这份资料'), '确认归档'));
  if (page === 'material-delete') return panel(form(note('资料及其解析内容将永久删除，无法恢复。关联的学习证据、测评和计划可能失效。', 'warning') + `<h2>当前引用这份资料的空间</h2>${data.affected.length ? `<ul class="workspace-list">${data.affected.map(space => `<li>${esc(space.name)}</li>`).join('')}</ul>` + check('cascade', 'on', '确认解除上述空间的资料引用，并处理相关学习记录', false, 'required') : empty('当前没有空间绑定这份资料。历史引用仍会由后端一并处理。')}<p class="subtle">后端会再次核对引用关系；若期间发生变化，请刷新后重新确认。</p>` + confirm(`我确认永久删除「${esc(material.name)}」`), '永久删除资料', true));
  if (page === 'material-versions') return panel(`<div class="panel-title"><h2>版本历史</h2>${link('上传新版本', 'material-upload', id, null, 'button button-primary')}</div>` + table(['文件与版本', '导入时间', '解析状态', '知识版本'], data.versions.map(v => `<tr><td><strong>${esc(v.filename)}</strong>${v.id === material.current_version_id ? '<span class="badge">当前</span>' : ''}<small class="workspace-id">${esc(v.id)}</small></td><td>${dateText(v.created_at)}</td><td>${label(v.status)}</td><td>${v.graph_version == null ? '尚未发布' : esc(v.graph_version)}</td></tr>`).join('')) + note('上传和发布新版本后，已有空间仍保留原知识版本；请在空间的“知识更新”中确认采用。'));
  if (page === 'material-upload') return panel(form(`<p class="subtle">为「${esc(material.name)}」上传更新内容，旧版本会保留。</p>${field('选择新文件', 'file', '', 'type="file" accept=".pdf,.md,.txt" required')}<p class="subtle">PDF、Markdown 或 TXT，最大 20 MB。</p>${area('更新说明（选填）', 'note', '', 'maxlength="2000"')}`, '上传新版本') + (data.uploaded ? note('新版本已接收。' + link('查看版本状态', 'material-versions', id)) : ''));
  if (page === 'material-ingest' || page === 'material-reconcile') return panel(form(note(page === 'material-ingest' ? '重新处理所选版本的原始文件。完成后仍需审核知识图谱。' : '基于已解析内容重新核对图谱，生成待审核的候选，不会直接覆盖空间中的知识。') + versionChoice(data), '开始处理') + `<div class="workspace-related">${link('进入图谱审核', 'material-graph', id)}</div>`);
  if (page === 'material-graph') {
    const diff = data.diff;
    if (!diff.candidate_revision_id) return panel(empty(diff.base_graph_version ? '当前图谱已发布，没有待审核的修订。' : '尚未生成候选图谱，请检查资料解析状态。'));
    return panel(`<p class="subtle">${label(diff.status)} · 当前知识版本 ${esc(diff.base_graph_version)}</p>${diffView(diff)}` + (diff.status === 'pending_review' && !diff.correction_id ? form(rows(diff.conflicts).map((conflict, i) => `<section class="workspace-conflict"><h3>冲突 ${i + 1} · ${esc(title(conflict.after || conflict.before))}</h3>${compare(conflict.before, conflict.after)}${select('处理方式', 'conflict-' + i, [['', '请选择'], ['keep_old', '保留原有内容'], ['use_new', '采用新版内容'], ['keep_both', '保留两种内容']])}${area('处理理由', 'reason-' + i)}</section>`).join('') + confirm('已核对变更和全部冲突，确认发布本次知识图谱'), '确认发布') : note(diff.correction_id ? '这是知识纠错候选，请从对应学习空间的“知识纠错”确认发布。' : '当前修订尚不可发布，请稍后刷新。')));
  }
}
function compare(before, after) {
  const describe = row => !row ? '无' : [title(row), row.description, row.type ? `关系：${row.type}` : '', row.from_id ? `${row.from_id} → ${row.to_id}` : ''].filter(Boolean).map(esc).join('<br>');
  return `<div class="workspace-compare"><div><small>原内容</small><p>${describe(before)}</p></div><div><small>新内容</small><p>${describe(after)}</p></div></div>`;
}
function diffView(diff) {
  return `<div class="workspace-counts"><span>新增 <b>${rows(diff.added).length}</b></span><span>修改 <b>${rows(diff.changed).length}</b></span><span>移除 <b>${rows(diff.removed).length}</b></span><span>冲突 <b>${rows(diff.conflicts).length}</b></span></div>` + [['added', '新增内容'], ['changed', '修改内容'], ['removed', '移除内容']].map(([key, text]) => rows(diff[key]).length ? `<details class="workspace-details"><summary>${text}（${diff[key].length}）</summary>${diff[key].map(row => `<article><span class="badge">${({ node: '知识点', relation: '关系', source: '来源' })[row.kind] || esc(row.kind)}</span>${compare(row.before, row.after)}</article>`).join('')}</details>` : '').join('');
}
function assessmentView(page, id, item, data) {
  const results = rows(data.result.question_results), questions = rows(data.assessment.questions);
  const question = questions.find(q => q.id === item);
  const result = results.find(q => q.question_id === item);
  if (page === 'assessment-result') return panel((data.result.notes?.chapter_id ? `<p><a class="button button-secondary" href="#/space/${encodeURIComponent(data.space.id)}/notes?chapter=${encodeURIComponent(data.result.notes.chapter_id)}">查看本次学习笔记</a></p>` : '') + `<div class="panel-title"><div><h2>本轮作答反馈</h2><p class="subtle">${dateText(data.result.graded_at)} · ${results.length} 道题</p></div>${link('题目复核记录', 'assessment-reviews', id)}</div>` + (results.length ? results.map((result, index) => {
    const question = questions.find(q => q.id === result.question_id), answer = rows(data.assessment.answers).find(a => a.question_id === result.question_id);
    const answerText = rows(question?.options).find(option => option.id === answer?.answer)?.text || answer?.answer || '未作答';
    const blocked = ['pending', 'invalid'].includes(result.question_review_status);
    return `<article class="workspace-question"><div class="workspace-question-title"><h3>${index + 1}. ${esc(question?.prompt || '题目内容暂不可用')}</h3><span class="badge">${label(result.verdict)} · ${score(result.score)}</span></div>${rows(question?.options).length ? `<ol class="workspace-options">${question.options.map(option => `<li>${esc(option.text)}</li>`).join('')}</ol>` : ''}<p><strong>你的回答：</strong>${esc(answerText)}</p><div class="workspace-feedback">${esc(result.feedback || '暂无补充反馈')}</div><p class="subtle">${result.assisted ? '本题有辅助记录' : '独立作答记录'}${result.question_review_status ? ' · ' + label(result.question_review_status) : ''}</p><div class="workspace-links">${sourceButtons(result.source_refs)}${!blocked ? link('申请评分复核', 'assessment-grade', id, result.question_id) : ''}${result.question_review_status !== 'pending' ? link('反馈题目问题', 'assessment-report', id, result.question_id) : link('查看处理进度', 'assessment-reviews', id)}</div></article>`;
  }).join('') : empty('暂无逐题结果。')) + `<h2 class="workspace-section-title">各主题表现</h2>${table(['知识主题', '本轮均分', '已验证', '未验证'], rows(data.result.topic_results).map(row => `<tr><td>${esc(topicName(data, row.topic_id))}</td><td>${score(row.score)}</td><td>${esc(row.verified_count)}</td><td>${esc(row.unverified_count)}</td></tr>`).join(''))}<p class="subtle">本轮得分描述这次作答，是否掌握还需结合独立证据与后续复习。</p>`);
  if (['assessment-grade', 'assessment-report'].includes(page)) {
    if (!question) return panel(empty('题目不存在，请返回详细结果重新选择。'));
    const blocked = page === 'assessment-grade' ? ['pending', 'invalid'].includes(result?.question_review_status) : result?.question_review_status === 'pending';
    return panel(`<h2>${esc(question.prompt)}</h2><p class="workspace-feedback">${esc(result?.feedback || '')}</p>` + (blocked ? note('这道题当前不可重复提交此操作，请查看复核记录。') : form(note(page === 'assessment-grade' ? '如果你认为评分不准确，请说明原因。系统会依据当前题目与评分标准重新核对，并保留复核记录。' : '如果题目、选项或依据有问题，请在这里反馈。提交后该题将暂时退出有效评估，待核对来源后处理。') + reasonField(), page === 'assessment-grade' ? '申请评分复核' : '提交题目反馈')) + `<div class="workspace-related">${link('返回详细结果', 'assessment-result', id)}${link('查看复核记录', 'assessment-reviews', id)}</div>`);
  }
  if (page === 'assessment-reviews') return panel(rows(data.reviews.items).length ? data.reviews.items.map(review => `<article class="workspace-question"><div class="workspace-question-title"><h2>${esc(review.question?.prompt || '题目反馈')}</h2><span class="badge">${label(review.status)}</span></div><ol class="workspace-timeline">${rows(review.events).map(event => `<li><strong>${label(event.action)} · ${dateText(event.created_at)}</strong><p>${esc(event.reason)}</p></li>`).join('')}</ol>${review.status === 'pending' && !rows(review.events).some(e => e.action !== 'report') ? link('核对来源并处理', 'assessment-resolve', id, review.review_id, 'button button-secondary') : ''}</article>`).join('') : empty('本轮还没有题目反馈。可从逐题结果提交问题。'));
  if (page === 'assessment-resolve') {
    const review = rows(data.reviews.items).find(row => row.review_id === item);
    if (!review) return panel(empty('未找到这条题目反馈。'));
    if (rows(review.events).some(e => e.action !== 'report')) return panel(note('这条反馈已处理。' + link('查看处理记录', 'assessment-reviews', id)));
    const question = review.question || review.frozen_question;
    return panel(`<h2>${esc(question.prompt)}</h2><p class="subtle">请先阅读出题时封存的原文，再选择处理方式。更正与作废会更新评分和相关学习证据。</p><details class="workspace-details" open><summary>封存的来源原文</summary><div class="workspace-source-text">${esc(review.source_text || '未返回来源正文，暂不能处理。')}</div></details>` + form(`<fieldset class="form-field"><legend>本次采用的来源依据</legend>${rows(review.frozen_question.source_refs).map((ref, index) => check('source', index, `来源 ${index + 1}${ref.page ? ' · 第 ' + esc(ref.page) + ' 页' : ''} · ${esc(rows(ref.section_path).join(' / '))}`)).join('')}</fieldset>${select('处理方式', 'action', [['', '请选择'], ['correct', '更正答案与评分标准'], ['invalidate', '作废这道题'], ['reject', '题目无误，驳回反馈']], '', 'required')}<div data-ws-when="action:correct">${question.type === 'single_choice' ? select('正确选项', 'answer_key', [['', '请选择正确选项'], ...rows(question.options).map(option => [option.id, option.text])], question.answer_key || '') : area('参考答案（选填）', 'answer_key', question.answer_key || '', 'maxlength="8000"')}${area('完整评分标准', 'rubric', question.rubric || '', 'required maxlength="8000"')}</div>${reasonField()}${confirm('我已核对封存来源及处理内容，确认提交本次裁决')}`, '确认处理'));
  }
}
function spaceView(page, id, data, filters) {
  const space = data.space;
  if (page === 'space-settings') return panel(menu([
    ['空间名称', '让学习目标更容易识别', 'space-name', id], ['目标与偏好', '调整学习目标与讲解方式', 'space-profile', id],
    ['导出记录', '下载包含学习空间数据的 ZIP', 'export', id], ['重置学习状态', '选择需要重新积累证据的主题', 'learning-reset', id],
  ]));
  if (page === 'space-name') return panel(form(field('空间名称', 'name', space.name, 'required maxlength="100"')));
  if (page === 'space-profile') {
    const profile = data.profile.profile || {}, value = key => profile[key]?.value, preferences = value('preferences') || {};
    return panel(form(area('学习目标', 'goal', value('goal') || space.goal || '', 'required maxlength="1000"')
      + `<div class="field-row">${field('每周学习时间（分钟，选填）', 'weekly_minutes', value('weekly_minutes') ?? '', 'type="number" min="15" max="2400" step="1"')}${field('目标日期（选填）', 'target_date', value('target_date') || '', 'type="date"')}</div>`
      + `<fieldset class="form-field"><legend>讲解偏好</legend>${check('example_first', 'on', '先举例，再解释概念', preferences.example_first)}${check('concise_explanations', 'on', '解释简洁，突出重点', preferences.concise_explanations)}</fieldset>`, '保存目标与偏好'));
  }
  if (page === 'space-scope') return panel(form(`<p class="subtle">选中想学习的主题；“暂时排除”用于控制前置主题的自动纳入。</p>${table(['知识主题', '纳入学习', '暂时排除'], rows(data.topics).map(topic => `<tr><th scope="row">${esc(title(topic))}</th><td>${check('topic_ids', topic.id, '<span class="screenreader">纳入 ' + esc(title(topic)) + '</span>', rows(space.topic_ids).includes(topic.id))}</td><td>${check('excluded_topic_ids', topic.id, '<span class="screenreader">排除 ' + esc(title(topic)) + '</span>', rows(space.excluded_topic_ids).includes(topic.id))}</td></tr>`).join(''))}${check('prerequisites', 'on', '自动纳入必要的前置知识', true)}`, '保存学习范围'));
  if (page === 'knowledge-updates') {
    const updates = data.updates;
    return panel(!rows(updates.available_updates).length ? empty('当前空间已经采用可用的最新知识。') : form(`<p class="subtle">新版资料发布后，需要在这里确认采用。未选择的资料继续保留原版本。</p>${rows(updates.available_updates).map(update => check('materials', update.material_id, `${esc(update.material_name || '资料 ' + (rows(space.bindings).findIndex(b => b.material_id === update.material_id) + 1))} · 知识版本 ${esc(update.previous_graph_version)} → ${esc(update.graph_version)}`, true)).join('')}<div class="workspace-counts"><span>可能受影响的主题 <b>${rows(updates.affected_topic_ids).length}</b></span><span>可能失效的题目 <b>${rows(updates.invalidated_question_ids).length}</b></span></div>${note(`以上为全部可用更新的影响预览。${updates.plan_impact ? '相关学习安排需要重新生成。' : '采用后请核对学习状态。'}`)}${confirm('已了解影响，确认采用勾选的知识版本')}`, '采用所选更新')) + `<div class="workspace-related">${link('查看变更记录', 'knowledge-changes', id)}</div>`;
  }
  if (page === 'knowledge-changes') return panel(rows(data.history.items).length ? `<ol class="workspace-timeline">${data.history.items.map(row => `<li><strong>${label(row.kind || row.type || row.action)} · ${dateText(row.created_at)}</strong>${row.reason ? `<p>${esc(row.reason)}</p>` : ''}<p class="subtle">${rows(row.affected_topic_ids).length} 个主题受影响</p>${row.from_graph_version != null || row.to_graph_version != null ? `<p>知识版本 ${esc(row.from_graph_version ?? '—')} → ${esc(row.to_graph_version ?? '—')}</p>` : ''}</li>`).join('')}</ol>${data.history.next_cursor ? action('加载更多记录', 'more') : ''}` : empty('暂无知识变更记录。'));
  if (page === 'export') return panel(`<h2>保留一份自己的学习记录</h2><p class="workspace-empty">下载 ZIP 文件，内含学习空间的 JSON 数据。原始 PDF 文件需自行保留。</p>${action('生成并下载', 'export', '', 'button button-primary')}${data.exportId ? action('重新下载上次导出', 'download') : ''}`);
  if (['learning-state', 'evidence'].includes(page)) {
    const filter = `<form data-workspace-filter><div class="workspace-filters">${select('知识主题', 'topic_id', [['', '全部主题'], ...rows(data.topics).map(topic => [topic.id, title(topic)])], filters.topic_id)}${page === 'evidence' ? select('证据类型', 'kind', [['', '全部类型'], ['objective_answer', '客观题作答'], ['short_answer', '简答题作答']], filters.kind) + field('开始时间', 'from', localDate(filters.from), 'type="datetime-local"') + field('结束时间', 'to', localDate(filters.to), 'type="datetime-local"') : select('学习状态', 'status', [['', '全部状态'], ['unseen', '尚未学习'], ['learning', '学习中'], ['mastered', '已掌握'], ['unstable', '掌握不稳定'], ['needs_review', '需要复习']], filters.status)}<button type="submit" class="button button-secondary">筛选</button></div></form>`;
    if (page === 'learning-state') return panel(filter + (rows(data.state.items).length ? table(['知识主题', '当前状态', '有效证据均值', '证据数量'], data.state.items.map(row => `<tr><td>${esc(topicName(data, row.topic_id))}</td><td>${label(row.status)}${row.score_validity === 'stale' ? ' · 版本已变化' : ''}</td><td>${row.score_validity === 'stale' ? '需重新评估' : score(row.mastery_score)}</td><td>${rows(row.evidence_ids || row.evidence).length}</td></tr>`).join('')) : empty('没有符合条件的学习状态。')) + `<div class="workspace-related">${link('查看证据明细', 'evidence', id)}${link('重置部分主题', 'learning-reset', id)}</div>`);
    return panel(filter + (rows(data.evidence.items).length ? data.evidence.items.map(row => `<article class="workspace-question"><div class="workspace-question-title"><h3>${esc(topicName(data, row.topic_id))}</h3><span class="badge">${label(row.kind)}</span></div><p>${dateText(row.observed_at || row.created_at)} · ${label(row.result)} · 分数 ${score(row.score)}</p><p class="subtle">${row.assisted ? '有辅助记录' : '无辅助标记'} · ${row.revoked_by_review_id ? '已被复核撤销' : row.eligible ? '可用于评估' : '不计入当前评估'}${rows(row.error_tags).length ? ' · ' + rows(row.error_tags).map(esc).join('、') : ''}</p><div class="workspace-links">${sourceButtons(row.source_refs)}${row.assessment_id ? link('查看本轮测评', 'assessment-result', row.assessment_id) : ''}</div></article>`).join('') + (data.evidence.next_cursor ? action('加载更多证据', 'more') : '') : empty('没有符合条件的学习证据。')));
  }
  if (page === 'learning-reset') return panel(form(note('重置后，所选主题需要重新积累当前有效证据。历史记录会保留，重置会记录原因。', 'warning') + `<fieldset class="form-field"><legend>选择要重置的主题</legend><div class="workspace-topic-choices">${rows(data.topics).map(topic => check('topic_ids', topic.id, esc(title(topic)))).join('')}</div></fieldset>${reasonField()}${confirm('确认重置勾选主题的当前学习状态')}`, '重置所选主题', true));
}
const localDate = iso => { if (!iso) return ''; const date = new Date(iso); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
export function correctionFields(data, key) {
  const [kind, id] = String(key || '').split(':'), target = rows(kind === 'relation' ? data.graph?.edges : data.graph?.nodes).find(row => row.id === id);
  if (!target) return empty('先选择要纠错的知识点或关系。');
  const names = rows(data.graph.nodes).map(row => [row.id, title(row)]);
  return `<div class="workspace-links">${sourceButtons(target.source_refs)}</div>${select('原文依据', 'source', [['', '请选择已核对的来源'], ...rows(target.source_refs).map((ref, i) => [ref.chunk_id, `来源 ${i + 1}${ref.page ? ' · 第 ' + ref.page + ' 页' : ''}`])], '', 'required')}<div data-ws-when="action:replace">${kind === 'node' ? field('更正后的名称', 'name', target.name || target.title || '', 'maxlength="255"') + area('更正后的解释', 'description', target.description || '', 'maxlength="10000"') : select('起点', 'from_id', names, target.from_id) + select('终点', 'to_id', names, target.to_id) + select('关系', 'type', [['contains', '包含'], ['prerequisite_of', '是其前置知识'], ['related_to', '相关'], ['assessed_by', '由其测评'], ['explained_by', '由其解释'], ['supersedes', '替代'], ['contradicts', '相互矛盾']], target.type)}</div>`;
}
function correctionView(data) {
  if (data.correctionPublished) return panel('<h2>纠错已发布</h2><p class="workspace-empty">核对知识更新后，在当前空间采用新版本。采用后可以继续纠错。</p>' + link('查看知识更新', 'knowledge-updates', data.space.id, null, 'button button-primary'));
  if (data.correction) return panel(`<h2>核对纠错候选</h2><p>${esc(data.correction.body.reason)}</p>${diffView(data.diff)}${data.diff.status === 'draft' ? form(reasonField() + confirm('已核对上述更改与来源，确认发布纠错'), '确认发布纠错') : note('候选正在准备或已处理，请刷新查看最新状态。')}<p class="subtle">发布后，进入“知识更新”确认当前空间采用的版本。</p>`);
  const choices = [...rows(data.graph.nodes).map(row => ['node:' + row.id, '知识点 · ' + title(row)]), ...rows(data.graph.edges).filter(row => rows(row.source_refs).length).map(row => ['relation:' + row.id, `关系 · ${topicName(data, row.from_id)} → ${topicName(data, row.to_id)}`])];
  return panel(form(note('先核对原文，再提交纠错候选。下一步会显示变更，确认后才发布。') + select('知识点或关系', 'target', [['', '请选择'], ...choices], '', 'required data-workspace-target') + select('纠错方式', 'action', [['replace', '更正内容'], ['reject', '拒绝这个知识项']]) + '<div data-workspace-correction-fields>' + empty('选择后可查看来源并填写更正内容。') + '</div>' + reasonField(), '生成纠错候选'));
}
function taskView(page, id, item, data) {
  const task = data.task, plan = data.plan;
  const historical = task.historical || task.context?.historical || !['ready', 'needs_replan'].includes(plan.status);
  const canStart = !historical && plan.status === 'ready' && ['pending', 'in_progress'].includes(task.status);
  const heading = `<h2>${esc(task.title || task.name || task.context?.title || rows(task.topic_ids).map(id => topicName(data, id)).join('、') || '学习任务')}</h2><p class="subtle">${label(task.status)} · ${esc(task.estimated_minutes ?? task.minutes ?? '—')} 分钟</p>`;
  if (page === 'task') {
    if (historical) return panel(heading + note('这是历史任务，保留当时的状态与学习记录。') + `<p>${esc(task.note || '未记录学习备注。')}</p>`);
    return panel(heading + form(select('处理方式', 'status', [['completed', '标记完成'], ['skipped', '跳过这次任务'], ['deferred', '延期安排']], ['completed', 'skipped', 'deferred'].includes(task.status) ? task.status : 'completed') + `<div data-ws-when="status:deferred">${field('延期到', 'defer_until', localDate(task.defer_until), 'type="datetime-local" required')}</div>` + area('原因（跳过时必填）', 'reason', task.reason || '', 'maxlength="2000"') + area('学习备注（选填）', 'note', task.note || '', 'maxlength="2000"'), '保存任务') + (canStart ? `<div class="workspace-related">${link('开始任务学习', 'session', id, item)}</div>` : ''));
  }
  const session = data.session, same = session?.task_id === item && session?.plan_id === id;
  if (session?.status === 'active' && !same) return panel(heading + note('当前空间还有一个进行中的学习会话，请先返回该会话。') + `<a class="button button-primary" href="${esc(session.route)}">返回当前会话</a>`);
  if (same && session.status === 'finished') {
    let assessment;
    try { assessment = taskAssessmentContext(plan, item, { sessionId: session.session_id || session.id }); } catch { /* History retains records without offering new assessments. */ }
    const duration = session.elapsed_seconds == null ? '未记录起止间隔。' : `起止间隔 ${Math.max(0, Math.round(Number(session.elapsed_seconds) / 60))} 分钟。该时间包含暂停，不等同于专注时长。`;
    return panel(heading + note(`本次会话已保存，${duration}`) + `<div class="workspace-links">${assessment ? `<button type="button" class="button button-primary" data-action="task-assessment" data-plan="${esc(id)}" data-task="${esc(item)}" data-session="${esc(session.session_id || session.id || '')}" data-kind="${assessment.kind}" data-topics="${esc(JSON.stringify(rows(task.topic_ids)))}">${assessment.kind === 'retest' ? '复测本次学习' : '测评本次学习'}</button>` : ''}${!historical ? link('记录任务完成情况', 'task', id, item, 'button button-secondary') : ''}${canStart ? action('再开始一次学习', 'session-start') : ''}</div>`);
  }
  if (!same || session.status !== 'active') {
    if (!canStart) return panel(heading + empty('当前任务不能启动新的学习会话，请查看历史记录或重新规划。') + link('查看任务安排', 'task', id, item));
    return panel(heading + empty('开始后，查看资料、请求讲解及暂停操作会记录到这个任务。结束时保存会话。') + action('开始学习并记录', 'session-start', '', 'button button-primary'));
  }
  return panel(heading + `<div class="workspace-session"><span class="workspace-session-dot ${session.paused ? 'paused' : ''}"></span><h3>${session.paused ? '已暂停' : '正在学习'}</h3><p>开始于 ${dateText(session.started_at)}</p></div><div class="workspace-links">${action(session.paused ? '继续学习' : '暂停', 'session-event', session.paused ? 'resume' : 'pause')}${action('结束并保存', 'session-finish', '', 'button button-primary')}</div><div class="workspace-session-resources"><h3>学习支持</h3><div class="workspace-links">${action('打开任务资料', 'session-event', 'open_material')}${action('请求讲解', 'session-event', 'request_explanation')}${action('获得提示', 'session-event', 'request_hint')}</div></div><p class="subtle">离开此页后，可从空间内的学习安排入口回到会话；关闭标签页前请结束并保存。</p>`);
}
export function renderWorkspace(view, { returnPath = view.returnPath } = {}) {
  const { page, id, item } = view.route, data = view.data;
  const deletionPending = page === 'material-delete' && Boolean(view.pendingRun) && !data;
  const heading = workspaceTitles[page] || '功能页面';
  const spaceId = data?.space?.id || (!page.startsWith('assessment') && !['task', 'session'].includes(page) ? id : '');
  const back = returnPath || (page === 'material' ? '#/materials' : page.startsWith('material') ? workspacePath('material', id) : page === 'health' ? '#/overview'
    : spaceId ? '#/space/' + encodeURIComponent(spaceId) + '/' + workspaceSection(page) : '#/spaces');
  let content = '';
  if (deletionPending) content = panel(note('删除请求已受理。资料记录已经移出资料库，后台正在清理原文件、解析索引和知识图谱。', 'warning') + '<p class="subtle">清理完成前请不要重复提交删除；如果长时间没有完成，请启动 graph worker 后点击“查看原任务结果”。</p>');
  else if (!data) content = panel(view.busy ? '<div class="loading-block" role="status">正在读取…</div>' : empty('暂未读取到内容，可点击刷新重试。'));
  else if (page.startsWith('material')) content = materialView(page, id, data);
  else if (page.startsWith('assessment')) content = assessmentView(page, id, item, data);
  else if (['task', 'session'].includes(page)) content = taskView(page, id, item, data);
  else if (page === 'knowledge-correct') content = correctionView(data);
  else if (page === 'health') {
    const health = data.health, deps = health.dependencies;
    content = panel(`<h2>本地服务 · ${label(health.status)}</h2>` + (!deps ? note('服务可连接，但未返回依赖详情。请刷新自动连接后重试。') : table(['服务', '状态', '说明'], [['mysql', '学习数据'], ['neo4j', '知识图谱'], ['qdrant', '资料检索'], ['llm', '出题与问答'], ['embedding', '文本向量']].map(([key, name]) => { const value = deps[key]; return `<tr><td>${name}</td><td>${label(typeof value === 'object' ? value?.status : value)}</td><td>${typeof value === 'object' ? esc([value.provider, value.model].filter(Boolean).join(' · ')) : '—'}</td></tr>`; }).join('')) + '<p class="subtle">模型“已配置”表示已设置凭据，不代表本轮出题一定成功。</p>'));
  } else content = spaceView(page, id, data, view.filters);
  const displayError = deletionPending ? '' : view.error;
  return `<div class="workspace-page" data-workspace-key="${esc(view.key + ':' + view.editRevision)}"><a class="text-link back-link" href="${esc(back)}">← 返回</a><div class="page-heading"><div><h1>${heading}</h1><p>${esc(data?.material?.name || data?.space?.name || '把需要的操作，放在清楚的位置。')}</p></div>${action('刷新', 'reload')}</div><div class="workspace-feedback-area" aria-live="polite">${displayError ? `<p class="notice error" role="alert">${esc(displayError)}</p>` : ''}${view.notice ? note(esc(view.notice)) : ''}${view.pendingRun && !view.busy ? '<div class="workspace-related"><span class="subtle">上次后台任务尚待确认</span>' + action('查看原任务结果', 'poll-command') + '</div>' : ''}${view.busy && data ? `<p class="notice" role="status">${esc(view.busy)}</p>` : ''}</div><div class="workspace-body" aria-busy="${Boolean(view.busy)}">${content || panel(empty('该功能页面不存在。'))}${page === 'knowledge-correct' ? '<div class="workspace-related">' + link('检查空间的知识更新', 'knowledge-updates', id) + '</div>' : ''}</div></div>`;
}

export function updateWorkspaceConditions(root, view) {
  const busy = Boolean(view.busy);
  root.querySelectorAll('[data-ws-when]').forEach(section => {
    const [name, value] = section.dataset.wsWhen.split(':');
    const selected = section.closest('form')?.elements.namedItem(name)?.value;
    section.hidden = selected !== value;
    section.querySelectorAll('input,textarea,select').forEach(control => { control.disabled = busy || section.hidden; });
  });
  root.querySelectorAll('.workspace-fields').forEach(fieldset => { fieldset.disabled = busy; });
  root.querySelectorAll('[data-workspace-action], [data-workspace-filter] button').forEach(button => { button.disabled = busy; });
}
