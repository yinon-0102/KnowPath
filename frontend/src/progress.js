import { esc } from './store.js';

const rows = value => Array.isArray(value) ? value : [];
const present = value => value !== undefined && value !== null && value !== '';
const finite = value => present(value) && Number.isFinite(Number(value));
const id = value => encodeURIComponent(String(value ?? ''));
const taskName = task => task?.title || task?.context?.title || task?.name || '未命名任务';
const taskStates = { completed: '已完成', pending: '待学习', active: '学习中', skipped: '已跳过', deferred: '已延期' };
const labels = { ...taskStates, ready: '可学习', in_progress: '学习中', needs_replan: '需更新计划', superseded: '已有更新计划', stale: '需更新计划', archived: '已归档', finished: '已结束', paused: '已暂停', failed: '失败', cancelled: '已取消', generating: '正在出题', grading: '正在评分', diagnostic: '诊断测评', practice: '练习测评', retest: '复测', learn: '学习', review: '复习', correct: '正确', incorrect: '错误', unverified: '未验证', partial: '部分正确', invalid: '已作废' };
const label = value => labels[value] || value || '未记录';
const unavailablePlan = status => ['superseded', 'stale', 'archived', 'cancelled', 'failed'].includes(status);

function sortedNodes(nodes, schedule = false) {
  return rows(nodes).filter(Boolean).map((node, index) => ({ node, index })).sort((a, b) => {
    const order = node => schedule ? node.context?.position ?? node.position ?? node.sequence : node.sequence;
    const first = finite(order(a.node)) ? Number(order(a.node)) : Infinity;
    const second = finite(order(b.node)) ? Number(order(b.node)) : Infinity;
    return first - second || a.index - b.index;
  }).map(item => item.node);
}

function chronologicalRounds(progress) {
  return rows(progress?.rounds).filter(Boolean).map((round, index) => ({ round, index })).sort((a, b) => {
    const first = Date.parse(a.round.created_at), second = Date.parse(b.round.created_at);
    return Number.isFinite(first) && Number.isFinite(second) && first !== second ? first - second : b.index - a.index;
  }).map(item => item.round);
}

function currentRound(progress, currentPlan) {
  const rounds = chronologicalRounds(progress);
  if (currentPlan) return rounds.find(round => round.plan_id === (currentPlan.id || currentPlan.plan_id)) || { plan_id: currentPlan.id || currentPlan.plan_id, created_at: currentPlan.created_at, status: currentPlan.status, nodes: [] };
  return [...rounds].reverse().find(round => !unavailablePlan(round.status)) || null;
}

// A carried task can live in an older origin round in the cumulative response.
// The current plan remains authoritative for its task id, sequence and state.
export function currentProgressNodes(progress, currentPlan = null) {
  const round = currentRound(progress, currentPlan);
  if (!currentPlan) return sortedNodes(round?.nodes);
  const nodes = rows(progress?.rounds).flatMap(item => rows(item.nodes));
  const byNode = new Map(nodes.filter(node => present(node?.node_id)).map(node => [node.node_id, node]));
  const planId = currentPlan.id || currentPlan.plan_id;
  return sortedNodes(rows(currentPlan.tasks).filter(task => task && !task.historical && !task.context?.historical).map(task => {
    const taskId = task.id || task.task_id;
    const nodeId = task.node_id || task.context?.node_id || taskId;
    const match = byNode.get(nodeId) || nodes.find(node => node.plan_id === planId && node.task_id === taskId) || {};
    return { ...match, ...task, node_id: nodeId, sequence: task.sequence ?? task.context?.sequence ?? match.sequence, plan_id: planId, task_id: taskId, title: task.title || task.context?.title || task.name || match.title, historical: false };
  }), true);
}

function nodeStrip(nodes, heading) {
  if (!nodes.length) return '<p class="progress-empty">暂无学习节点</p>';
  return `<div class="progress-scroll" role="region" aria-label="${esc(heading)}节点，可横向滚动" tabindex="0"><ol class="progress-track">${nodes.map((node, index) => {
    const state = ['completed', 'pending', 'skipped', 'deferred'].includes(node.status) ? node.status : 'pending';
    const active = present(node.active_session_id);
    const status = active && state !== 'completed' ? '学习中' + (state === 'pending' || state === 'active' ? '' : ' · ' + label(node.status)) : label(node.status);
    const ordinal = finite(node.sequence) && Number(node.sequence) > 0 ? Number(node.sequence) : index + 1;
    const name = taskName(node);
    const minutes = finite(node.estimated_minutes) && Number(node.estimated_minutes) >= 0 ? ` · 预计 ${Number(node.estimated_minutes)} 分钟` : '';
    const accessible = `${ordinal}. ${name}，${status}${minutes}${node.historical ? '，历史任务' : ''}`;
    const connector = index ? `<span class="progress-connector ${nodes[index - 1].status === 'completed' && node.status === 'completed' ? 'completed' : 'pending'}" aria-hidden="true"></span>` : '';
    const glyph = state === 'completed' ? '<svg class="progress-check" viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' : state === 'skipped' ? '<span aria-hidden="true">−</span>' : state === 'deferred' ? '<span aria-hidden="true">↷</span>' : '<span class="progress-hollow" aria-hidden="true"></span>';
    return `<li class="progress-step">${connector}<button type="button" class="progress-node ${state}${active ? ' active' : ''}${node.selected ? ' selected' : ''}" data-action="progress-node" data-plan="${esc(node.plan_id)}" data-task="${esc(node.task_id)}" data-node="${esc(node.node_id)}" aria-label="${esc(accessible)}"${node.selected ? ' aria-pressed="true"' : ''}${!present(node.plan_id) || !present(node.task_id) ? ' disabled' : ''}><span class="progress-dot">${glyph}</span><span class="progress-node-title"><span class="progress-ordinal">${esc(ordinal)}.</span> ${esc(name)}</span><span class="progress-node-status">${esc(status)}${node.historical ? ' · 历史' : ''}</span></button></li>`;
  }).join('')}</ol></div>`;
}

const countText = nodes => `已完成 ${nodes.filter(node => node.status === 'completed').length} / ${nodes.length}`;
const timestamp = value => !present(value) ? '未记录' : `<time${Number.isFinite(Date.parse(value)) ? ` datetime="${esc(value)}"` : ''}>${esc(String(value).replace('T', ' ').replace(/Z$/, ' UTC'))}</time>`;

export function renderProgress(progress, { currentPlan = null, compact = false, expandedRounds = [] } = {}) {
  const current = currentRound(progress, currentPlan);
  const currentNodes = currentProgressNodes(progress, currentPlan);
  let html = `<div class="learning-node-progress${compact ? ' compact' : ''}"><section class="progress-current" data-progress-view="current" aria-label="当前计划进度"><div class="progress-heading"><h3>当前计划</h3>${current ? `<span>${esc(countText(currentNodes))}</span>` : ''}</div>${current ? nodeStrip(currentNodes, '当前计划') : '<p class="progress-empty">尚未生成当前计划，暂无学习节点</p>'}</section>`;
  if (!compact) {
    const rounds = chronologicalRounds(progress);
    const expanded = new Set(rows(expandedRounds));
    const counters = finite(progress?.completed_count) && finite(progress?.total_count) && Number(progress.completed_count) >= 0 && Number(progress.total_count) >= Number(progress.completed_count) ? `已完成 ${Number(progress.completed_count)} / ${Number(progress.total_count)}` : '';
    html += `<section class="progress-cumulative" data-progress-view="cumulative" aria-label="空间累计进度"><div class="progress-heading"><h3>空间累计</h3>${counters ? `<span>${esc(counters)}</span>` : ''}</div>${rounds.length ? rounds.map(round => {
      const isCurrent = round.plan_id === current?.plan_id;
      const nodes = sortedNodes(round.nodes);
      return `<details class="progress-round${isCurrent ? ' current' : ''}" data-round="${esc(round.plan_id)}"${isCurrent || expanded.has(round.plan_id) ? ' open' : ''}><summary><span>${isCurrent ? '当前轮次' : '历史轮次'} · ${timestamp(round.created_at)}</span><span>${esc(label(round.status))} · ${esc(countText(nodes))}</span></summary>${nodeStrip(nodes, '累计轮次')}</details>`;
    }).join('') : '<p class="progress-empty">暂无学习节点</p>'}${present(progress?.next_cursor) ? `<div class="progress-pagination"><button type="button" class="button button-secondary" data-action="progress-more" data-space="${esc(progress.space_id)}" data-cursor="${esc(progress.next_cursor)}">加载更多历史轮次</button></div>` : ''}</section>`;
  }
  return html + '</div>';
}

export function elapsedText(seconds) {
  if (!finite(seconds) || Number(seconds) < 0) return '未记录';
  const total = Math.round(Number(seconds));
  const minutes = Math.floor(total / 60), remainder = total % 60;
  return minutes ? `${minutes} 分${remainder ? ` ${remainder} 秒` : '钟'}` : `${remainder} 秒`;
}

function sourceReference(ref) {
  if (!ref || typeof ref !== 'object') return `<li>${esc(ref || '未记录')}</li>`;
  const parts = [ref.material_name || ref.filename || ref.material_id, present(ref.material_version_id) ? `版本 ${ref.material_version_id}` : '', present(ref.page) ? `第 ${ref.page} 页` : '', rows(ref.section_path).join(' / '), present(ref.chunk_id) ? `片段 ${ref.chunk_id}` : ''].filter(present);
  const allowed = ['material_id', 'material_version_id', 'chunk_id', 'page', 'line_start', 'line_end', 'section', 'section_path', 'source_unit_id', 'source_unit_ids', 'document_id'];
  const locator = Object.fromEntries(allowed.filter(key => present(ref[key])).map(key => [key, ref[key]]));
  const action = present(locator.material_id) && present(locator.material_version_id) && present(locator.chunk_id) ? `<button type="button" class="text-link record-source-button" data-action="record-source" data-ref="${esc(JSON.stringify(locator))}">查看来源原文</button>` : '';
  return `<li>${parts.length ? parts.map(esc).join(' · ') : '来源信息未记录'}${action}</li>`;
}

function topicRecord(topic) {
  const source = topic.context_source || topic.source_origin || topic.source;
  const origin = ['session_snapshot', 'frozen', 'session', 'snapshot'].includes(source) ? '会话封存资料' : source === 'task_snapshot' ? '任务封存资料' : ['current', 'current_material', 'fallback'].includes(source) || topic.from_current_materials === true ? '当前资料补充' : source === 'missing' ? '封存资料缺失' : '';
  const fallback = topic.fallback === true && origin !== '当前资料补充' ? ' · 当前资料补充' : '';
  return `<li class="record-topic"><strong>${esc(topic.name || topic.title || topic.id || '未命名主题')}</strong>${origin || fallback ? `<span class="record-source-origin">${origin}${fallback}</span>` : ''}${rows(topic.source_refs).length ? `<ul class="record-source-refs">${topic.source_refs.map(sourceReference).join('')}</ul>` : '<p class="record-empty">来源未记录</p>'}</li>`;
}

const resultScore = score => finite(score) && Number(score) >= 0 && Number(score) <= 1 ? `${Math.round(Number(score) * 100)}%` : '未验证';
function assessmentResult(result) {
  if (!result || typeof result !== 'object') return '<p class="record-empty">未出结果</p>';
  const questions = rows(result.question_results), topics = rows(result.topic_results);
  return `<details class="record-result"><summary>已保存的测评结果</summary>${questions.length ? `<ol>${questions.map((question, index) => `<li><strong>${esc(question.prompt || `第 ${index + 1} 题`)}</strong> · ${esc(label(question.verdict))} · ${resultScore(question.score)}${present(question.feedback) ? `<p>${esc(question.feedback)}</p>` : ''}</li>`).join('')}</ol>` : ''}${topics.length ? `<ul>${topics.map(topic => `<li>${esc(topic.name || topic.topic_id || '主题')} · ${resultScore(topic.score)}</li>`).join('')}</ul>` : ''}${!questions.length && !topics.length ? '<p class="record-empty">已保存结果，详情请查看测评结果页。</p>' : ''}</details>`;
}

export function renderLearningRecord(record, { busy = false, error = '' } = {}) {
  if (busy) return '<div class="learning-record" aria-busy="true"><p class="record-empty" role="status">正在加载学习记录…</p></div>';
  if (error) return `<div class="learning-record"><p class="notice error" role="alert">${esc(error)}</p></div>`;
  if (!record?.task) return '<div class="learning-record"><p class="record-empty">暂无学习记录</p></div>';
  const task = record.task;
  const planId = record.plan_id, taskId = record.task_id || task.id;
  const planStatus = record.plan_status || record.plan?.status;
  const current = record.current === true || record.current !== false && ['active', 'ready', 'needs_replan'].includes(planStatus);
  const historical = record.historical || task.historical || task.context?.historical;
  const executable = current && !historical && !unavailablePlan(planStatus) && present(planId) && present(taskId);
  const canStart = executable && (!present(planStatus) || ['ready', 'active'].includes(planStatus)) && ['pending', 'in_progress', 'active'].includes(task.status);
  const canAssess = executable && (task.status === 'completed' || planStatus !== 'needs_replan' && ['pending', 'in_progress', 'active'].includes(task.status));
  const minutes = task.estimated_minutes ?? task.minutes;
  const sessions = rows(record.sessions).filter(Boolean);
  const matching = sessions.filter(session => session.plan_id === planId && session.task_id === taskId).map((session, index) => ({ session, index })).sort((a, b) => {
    const first = Date.parse(a.session.started_at), second = Date.parse(b.session.started_at);
    return Number.isFinite(first) && Number.isFinite(second) && first !== second ? first - second : a.index - b.index;
  }).map(item => item.session);
  const latestSession = [...matching].reverse().find(session => session.status === 'active') || matching[matching.length - 1];
  const sessionId = latestSession?.session_id || latestSession?.id;
  const route = present(planId) && present(taskId) ? `${id(planId)}/${id(taskId)}` : '';
  const actions = `${route ? `<a class="button button-secondary" href="#/manage/task/${route}">查看任务记录</a>` : ''}${canStart || latestSession?.status === 'active' ? `<a class="button button-primary" href="#/manage/session/${route}">${latestSession?.status === 'active' ? '继续学习会话' : '开始学习会话'}</a>` : ''}${canAssess ? `<button type="button" class="button button-secondary" data-action="task-assessment" data-plan="${esc(planId)}" data-task="${esc(taskId)}" data-space="${esc(record.space_id)}" data-kind="${task.status === 'completed' ? 'retest' : 'practice'}"${present(sessionId) ? ` data-session="${esc(sessionId)}"` : ''}>开始关联测评</button>` : ''}`;
  return `<div class="learning-record"><header class="record-heading"><h2>${esc(taskName(task))}</h2><p>${esc(label(task.status))}${finite(minutes) && Number(minutes) >= 0 ? ` · 预计 ${Number(minutes)} 分钟` : ' · 预计时长未记录'}${!current || unavailablePlan(planStatus) || historical ? ' · 历史记录' : ''}</p></header><div class="record-actions">${actions}</div><section class="record-section" aria-label="学习笔记"><h3>学习笔记</h3><p class="record-note">${esc(task.note || '未记录学习笔记')}</p>${record.note?.chapter_id ? `<a class="text-link" href="#/space/${id(record.space_id)}/notes?chapter=${id(record.note.chapter_id)}">查看自动学习笔记</a>` : ''}</section><section class="record-section" aria-label="学习会话"><h3>学习会话</h3><p class="record-elapsed">累计起止间隔（包含暂停）：<strong>${esc(elapsedText(record.total_elapsed_seconds))}</strong></p>${sessions.length ? `<ol class="record-sessions">${sessions.map((session, index) => {
    const same = session.plan_id === planId && session.task_id === taskId;
    return `<li><div class="record-row-heading"><strong>会话 ${index + 1}</strong><span>${esc(session.paused ? '已暂停' : label(session.status))}</span></div><dl class="record-times"><div><dt>开始</dt><dd>${timestamp(session.started_at)}</dd></div><div><dt>结束</dt><dd>${timestamp(session.finished_at)}</dd></div><div><dt>起止间隔（包含暂停）</dt><dd>${esc(elapsedText(session.elapsed_seconds))}</dd></div></dl>${canStart && same && session.status === 'active' ? `<a class="text-link" href="#/manage/session/${route}">继续此会话</a>` : ''}</li>`;
  }).join('')}</ol>` : '<p class="record-empty">未记录学习会话</p>'}</section><section class="record-section" aria-label="关联测评"><h3>关联测评</h3>${rows(record.assessments).length ? `<ol class="record-assessments">${rows(record.assessments).filter(Boolean).map(assessment => `<li><div class="record-row-heading"><strong>${esc(label(assessment.kind))}</strong><span>${esc(label(assessment.status))}</span></div><p>${timestamp(assessment.created_at)}${present(assessment.learning_session_id) ? ` · 关联会话 ${esc(assessment.learning_session_id)}` : ' · 会话未关联'}</p>${assessmentResult(assessment.result)}${present(assessment.assessment_id) ? `<a class="text-link" href="#/manage/assessment-result/${id(assessment.assessment_id)}">查看测评结果</a>` : ''}</li>`).join('')}</ol>` : '<p class="record-empty">未关联测评</p>'}</section><section class="record-section" aria-label="学习主题与来源"><h3>学习主题与来源</h3>${rows(record.topics).length ? `<ul class="record-topics">${rows(record.topics).filter(Boolean).map(topicRecord).join('')}</ul>` : '<p class="record-empty">主题与来源未记录</p>'}</section></div>`;
}
