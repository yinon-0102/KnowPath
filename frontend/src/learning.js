import { esc, uid, storageRead, storageWrite } from './store.js';

const SAVED = 'knowpath-learning-assessments-v1';
const terminal = status => ['completed', 'failed', 'cancelled', 'stale'].includes(status);
const blank = (spaceId, mode) => ({ spaceId, mode, busy: '', error: '', assessmentId: '', diagnostic: null, result: null, evolution: null, comparison: null, replay: null, pendingAnswer: null });

export function comparisonPayload(budgets, horizon) {
  const values = String(budgets).split(/[,，]/).map(value => Number(value.trim()));
  const days = Number(horizon);
  if (values.length < 2 || values.length > 5 || new Set(values).size !== values.length || values.some(value => !Number.isInteger(value) || value < 1 || value > 480)) throw new Error('请输入 2—5 个互不相同的每日预算，每项为 1—480 分钟，用逗号分隔。');
  if (!Number.isInteger(days) || days < 1 || days > 30) throw new Error('比较窗口须为 1—30 个完整天数。');
  return { budgets_minutes_per_day: values, horizon_days: days };
}

export function createLearningController({ api, context, changed = () => {}, storage }) {
  let view = blank('', 'demo'), identity = '', generation = 0, activeRequest;
  const pendingKeys = new Map(), descriptors = new Map();
  function saved(spaceId) {
    if (descriptors.has(spaceId)) return { ...descriptors.get(spaceId) };
    const value = storageRead(storage, SAVED, {});
    const descriptor = value && typeof value === 'object' && !Array.isArray(value) && value[spaceId] && typeof value[spaceId] === 'object' ? { ...value[spaceId] } : {};
    descriptors.set(spaceId, descriptor);
    return { ...descriptor };
  }
  function save(spaceId, value) {
    descriptors.set(spaceId, { ...value });
    const prior = storageRead(storage, SAVED, {});
    const values = prior && typeof prior === 'object' && !Array.isArray(prior) ? prior : {};
    storageWrite(storage, SAVED, { ...values, [spaceId]: value });
  }
  function sync() {
    const current = context(), next = JSON.stringify([current.mode, current.spaceId, current.active !== false]);
    if (identity !== next) {
      activeRequest?.abort(); generation++; identity = next;
      view = blank(current.spaceId || '', current.mode);
      if (current.mode === 'live' && current.spaceId) {
        const persisted = saved(current.spaceId);
        view.assessmentId = typeof persisted.assessmentId === 'string' ? persisted.assessmentId : '';
        view.pendingAnswer = persisted.pendingAnswer || null;
      }
    }
    return view;
  }
  function live(ticket) { sync(); return ticket === generation && !activeRequest?.signal.aborted; }
  async function run(label, work) {
    sync();
    if (view.mode !== 'live' || !view.spaceId || context().active === false) {
      view.error = '连接本地服务并选择学习空间后，才能使用真实学习数据。'; changed(); return;
    }
    if (view.busy) return;
    const ticket = generation, spaceId = view.spaceId;
    activeRequest = new AbortController();
    const signal = activeRequest.signal;
    view.busy = label; view.error = ''; changed();
    try { await work({ ticket, spaceId, signal }); }
    catch (error) { if (live(ticket) && error.name !== 'AbortError') view.error = error.message || '操作未完成，请重试。'; }
    finally { if (live(ticket)) { view.busy = ''; changed(); } }
  }
  async function refresh(task, assessmentId) {
    if (!assessmentId) throw new Error('当前没有可恢复的诊断，请先开始诊断。');
    let assessment = await api.assessment(assessmentId, { signal: task.signal });
    if (!live(task.ticket)) return;
    if (assessment.space_id !== task.spaceId) { view.diagnostic = null; view.result = null; throw new Error('测验不属于当前学习空间，已停止显示。'); }
    if (assessment.status === 'generating') {
      await api.waitRun(assessment.run_id, task.signal);
      if (!live(task.ticket)) return;
      assessment = await api.assessment(assessmentId, { signal: task.signal });
      if (!live(task.ticket)) return;
      if (assessment.space_id !== task.spaceId) throw new Error('测验所属空间发生变化，请刷新。');
    }
    const diagnostic = await api.diagnostic(assessmentId, { signal: task.signal });
    if (!live(task.ticket)) return;
    if (diagnostic.assessment_id !== assessmentId) throw new Error('诊断数据与当前测验不一致，请刷新。');
    view.diagnostic = diagnostic; view.assessmentId = assessmentId;
    const descriptor = saved(task.spaceId);
    if (descriptor.pendingAnswer && diagnostic.current_question?.id !== descriptor.pendingAnswer.questionId) {
      delete descriptor.pendingAnswer; save(task.spaceId, descriptor);
    }
    view.pendingAnswer = descriptor.pendingAnswer || null;
    if (diagnostic.status === 'completed') {
      const result = await api.assessmentResult(assessmentId, { signal: task.signal });
      if (live(task.ticket)) view.result = result;
    }
  }
  async function start() {
    return run('正在生成并验证诊断题目…', async task => {
      if (view.assessmentId && !terminal(view.diagnostic?.status)) throw new Error('当前有一轮诊断，请先继续或刷新这轮诊断。');
      let descriptor = saved(task.spaceId);
      if (descriptor.assessmentId) descriptor = {};
      descriptor.createKey ||= uid(); save(task.spaceId, descriptor);
      const response = await api.createAssessment(task.spaceId, { adaptive: true, kind: 'diagnostic', question_count: 5, question_types: ['single_choice'] }, { signal: task.signal, key: descriptor.createKey });
      const assessmentId = response.assessment_id || response.assessment?.id;
      if (!assessmentId) throw new Error('服务未返回诊断标识，请使用相同请求重试。');
      descriptor = { assessmentId, runId: response.run_id }; save(task.spaceId, descriptor);
      if (!live(task.ticket)) return;
      view.assessmentId = assessmentId; view.diagnostic = null; view.result = null;
      await api.waitRun(response.run_id, task.signal);
      if (live(task.ticket)) await refresh(task, assessmentId);
    });
  }
  async function answer(value) {
    return run('正在记录回答并选择下一题…', async task => {
      const question = view.diagnostic?.current_question;
      if (!question) throw new Error('没有待提交的当前问题，请刷新诊断。');
      if (question.type === 'single_choice' && !(question.options || []).some(option => option.id === value)) throw new Error('请选择一个有效选项。');
      if (typeof value !== 'string' || !value.trim()) throw new Error('请先填写答案。');
      const descriptor = saved(task.spaceId);
      if (descriptor.pendingAnswer && descriptor.pendingAnswer.answer !== value) throw new Error('上次提交结果尚未确认，请先刷新诊断或重试原答案。');
      descriptor.pendingAnswer ||= { questionId: question.id, answer: value, key: uid() };
      save(task.spaceId, descriptor); view.pendingAnswer = descriptor.pendingAnswer;
      await api.recordAttempt(view.assessmentId, { answers: [{ question_id: question.id, expected_answer_revision: 0, answer: value }] }, { signal: task.signal, key: descriptor.pendingAnswer.key });
      if (live(task.ticket)) await refresh(task, descriptor.assessmentId);
    });
  }
  async function finalize() {
    return run('正在汇总已完成诊断…', async task => {
      if (!view.assessmentId || !view.diagnostic || view.diagnostic.current_question) throw new Error('请先完成当前问题，再汇总诊断。');
      const assessmentId = view.assessmentId;
      const result = await api.finalizeAssessment(assessmentId, { allow_unanswered: false }, { signal: task.signal });
      if (!live(task.ticket)) return;
      await api.waitRun(result.run_id, task.signal);
      if (live(task.ticket)) await refresh(task, assessmentId);
    });
  }
  async function report(field, label, request) {
    return run(label, async task => {
      const result = await request(task);
      if (!live(task.ticket)) return;
      if (result.space_id !== task.spaceId) throw new Error('报告与当前学习空间不一致，已停止显示。');
      view[field] = result;
    });
  }
  function retryKey(task, type, body) {
    const signature = JSON.stringify([task.spaceId, type, body]);
    if (!pendingKeys.has(signature)) pendingKeys.set(signature, uid());
    return { signature, key: pendingKeys.get(signature) };
  }
  return { sync, snapshot: () => sync(), start, answer, finalize,
    resume: () => run('正在恢复同一轮诊断…', task => refresh(task, view.assessmentId)),
    loadEvolution: () => report('evolution', '正在读取证据与复习时间…', task => api.evolution(task.spaceId, { signal: task.signal })),
    compare: body => {
      sync(); view.comparisonRequest = body;
      return report('comparison', '正在比较相同快照下的时间预算…', async task => {
      const retry = retryKey(task, 'comparison', body);
      const result = await api.comparePlans(task.spaceId, body, { signal: task.signal, key: retry.key });
      pendingKeys.delete(retry.signature); return result;
      });
    },
    replay: () => report('replay', '正在回放真实历史决策…', async task => {
      const retry = retryKey(task, 'replay', {});
      const result = await api.replayPolicies(task.spaceId, {}, { signal: task.signal, key: retry.key });
      pendingKeys.delete(retry.signature); return result;
    }),
    clear: () => { activeRequest?.abort(); generation++; identity = ''; pendingKeys.clear(); },
    dispose: () => { activeRequest?.abort(); generation++; },
  };
}

const number = value => Number.isFinite(value) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value) : '暂无';
const percent = value => Number.isFinite(value) ? `${number(value * 100)}%` : '暂无';
const time = value => { if (!value) return '暂无'; const date = new Date(value); return Number.isNaN(date.getTime()) ? '暂无' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date); };
const list = values => (Array.isArray(values) ? values : []);
const action = (label, name, disabled = false, primary = false) => `<button type="button" class="button ${primary ? 'button-primary' : 'button-secondary'}" data-action="learning-${name}" ${disabled ? 'disabled' : ''}>${label}</button>`;
const empty = text => `<p class="learning-empty">${text}</p>`;
const metric = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
const reasons = { insufficient_evidence: '独立证据不足', weak_topic: '需要加强的主题', topic_coverage: '覆盖学习主题', investigate_prerequisite: '独立排查已确认前置知识', no_available_prerequisite_question: '题池缺少可用前置题', prerequisite_out_of_scope: '前置主题不在本次范围内', requires_independent_verification: '等待独立作答验证', probe_not_independent_verified: '辅助或未验证答案不能支持假设', trigger_not_independent_incorrect: '触发答案已不构成独立错误证据', independent_prerequisite_observation: '依据独立前置题观察', no_current_independent_evidence: '暂无当前有效独立证据', independent_failure: '独立作答有误，缩短间隔', first_independent_success: '首次独立答对', spaced_independent_success: '间隔足够的独立答对', unspaced_success_does_not_extend: '间隔不足，不延后复习', easy_or_unknown_three_day_cap: '简单或难度未知题采用保守间隔', hard_application_required_for_fourteen_days: '更长间隔需要困难应用题', missing_prerequisite: '缺少必要前置', prerequisite_out_of_scope: '前置主题不在范围内', excluded_prerequisite: '前置主题已排除', horizon_capacity: '窗口容量不足', weekly_capacity: '每周预算不足', daily_capacity: '每日容量不足', blocked_prerequisite: '前置主题受阻', deadline: '截止日期限制', insufficient_prior_independent_evidence: '历史独立证据不足' };
const reason = value => esc(reasons[value] || value || '暂无');

function diagnosticSection(state, disabled) {
  const diagnostic = state.diagnostic, question = diagnostic?.current_question, done = diagnostic?.status === 'completed';
  const canStart = !state.assessmentId || terminal(diagnostic?.status);
  let content = '';
  if (diagnostic) {
    content += `<p class="learning-progress" role="status">已回答 ${number(diagnostic.progress?.answered)} / 目标 ${number(diagnostic.progress?.target)} 题 · 策略 ${esc(diagnostic.policy_version)}</p>`;
    if (question) {
      content += `<form id="learning-answer-form"><fieldset class="learning-options" ${disabled ? 'disabled' : ''}><legend>${esc(question.prompt)}</legend>${question.type === 'single_choice' ? list(question.options).map((option, index) => `<label class="learning-option"><input type="radio" name="answer" value="${esc(option.id)}" required ${state.pendingAnswer?.answer === option.id ? 'checked' : ''}><span><strong>${index + 1}.</strong> ${esc(option.text)}</span></label>`).join('') : '<label for="diagnostic-answer">你的回答</label><textarea id="diagnostic-answer" name="answer" maxlength="4000" required></textarea>'}</fieldset><button type="submit" class="button button-primary" ${disabled ? 'disabled' : ''}>提交当前答案</button></form>`;
    } else if (done) content += `<div class="notice" role="status">本轮诊断已完成。${number(list(state.result?.question_results).filter(r => r.verdict === 'correct').length)} 题正确；这不代表已经证明掌握。</div>`;
    else if (terminal(diagnostic.status)) content += `<div class="notice warning">本轮状态：${esc(({ failed: '生成失败', cancelled: '已取消', stale: '资料版本已变化' })[diagnostic.status] || diagnostic.status)}。请检查资料与连接后开始新一轮。</div>`;
    if (diagnostic.completion_reason === 'no_unseen_question_family') content += '<div class="notice warning">没有可用的新题族：已验证题池没有更多未使用题目。可以汇总现有回答，不会补造新题。</div>';
    if (diagnostic.completion_reason === 'exposure_history_limit') content += '<div class="notice warning">历史记录超出在线检查上限，无法确认候选题的独立性，本轮停止出题。只汇总已有记录，不据此判定已经掌握。</div>';
    if (Number(diagnostic.runtime_excluded_family_count) > 0) content += `<p class="subtle">为避免使用其他测验已展示的题目，已跳过 ${number(diagnostic.runtime_excluded_family_count)} 类题族。</p>`;
    if (!question && !terminal(diagnostic.status)) content += action('汇总诊断', 'finalize', disabled, true);
    if (list(diagnostic.decisions).length) content += `<details class="learning-details"><summary>查看逐题选择依据（${diagnostic.decisions.length} 步）</summary><ol>${diagnostic.decisions.map(d => `<li>第 ${number(d.step)} 步：${reason(d.reason)}<small>依据 ${number(list(d.evidence_ids).length)} 条历史证据 · ${number(list(d.attempt_ids).length)} 次当前作答</small></li>`).join('')}</ol></details>`;
    if (list(diagnostic.hypotheses).length) content += `<div class="learning-hypotheses"><h3>前置知识排查</h3>${diagnostic.hypotheses.map(h => `<p><span class="badge">${esc(({ pending: '待验证', supported: '存在缺口观察', not_supported: '本次未支持缺口', inconclusive: '证据不足' })[h.status] || h.status)}</span> ${reason(h.reason)}</p>`).join('')}<p class="subtle">前置关系与一次作答不能证明错误的因果关系。</p></div>`;
  } else content = empty(state.assessmentId ? '当前标签页保存了一轮诊断，点击“继续 / 刷新诊断”恢复。' : '还没有开始诊断。依据当前学习范围逐题作答，独立回答用于排查。');
  return `<section class="panel learning-panel" id="learning-diagnostic" aria-labelledby="diagnostic-title"><div class="learning-panel-heading"><span class="learning-step">01</span><div><h2 id="diagnostic-title">自适应诊断</h2><p>从证据不足处开始，一次看清一个问题。</p></div></div><div class="learning-actions">${action('开始新诊断', 'start', disabled || !canStart, true)}${action('继续 / 刷新诊断', 'resume', disabled || !state.assessmentId)}</div>${content}</section>`;
}

function evolutionSection(report, disabled) {
  const items = list(report?.items);
  const content = !report ? empty('读取真实作答、后续纠错与重置记录，查看当前复习安排。') : !items.length ? empty('暂无范围内的主题或学习证据；完成独立诊断后再查看。') : items.map(item => {
    const current = { ...item.current }, schedule = current.review_schedule || {}, curve = list(item.curve), events = list(item.events);
    if (current.score_validity === 'stale') {
      current.mastery_score = null;
      current.independent_evidence_count = list(schedule.selected_evidence_ids).length;
    }
    return `<article class="learning-topic"><h3>${esc(item.topic_name || item.topic_id)}</h3><dl class="learning-metrics">${metric('证据均值', number(current.mastery_score))}${metric('独立证据数', number(current.independent_evidence_count))}${metric('下次复习', time(schedule.next_review_at))}${metric('规则间隔', schedule.interval_days == null ? '暂无' : `${number(schedule.interval_days)} 天`)}</dl><p class="subtle">${schedule.due ? '已到复习时间' : '复习时间依据当前有效独立证据'} · ${list(schedule.reasons).map(reason).join('；') || '暂无有效独立证据'}</p>${curve.length ? `<details class="learning-details"><summary>查看掌握度变化（${curve.length} 个记录点）</summary><div class="learning-table-wrap"><table class="learning-table"><caption class="screenreader">实际作答时间与证据均值</caption><thead><tr><th scope="col">实际作答</th><th scope="col">当时可用均值</th><th scope="col">当前修订均值</th></tr></thead><tbody>${curve.map(point => `<tr><td>${time(point.observed_at)}</td><td>${number(point.observed_mastery_score)}</td><td>${number(point.mastery_score)}</td></tr>`).join('')}</tbody></table></div></details>` : empty('暂无有效曲线，不会填补虚构记录。')}${events.length ? `<details class="learning-details"><summary>作答、纠错与重置记录（${events.length} 条）</summary><ul>${events.map(event => `<li>${esc(({ observation: '原始作答', correction: '后续纠错', reset: '状态重置' })[event.type] || event.type)} · 记录于 ${time(event.recorded_at)}${event.observed_at ? `<small>作答于 ${time(event.observed_at)} · ${list(event.exclusion_reasons).map(reason).join('、') || '无显式排除标记'}</small>` : ''}</li>`).join('')}</ul></details>` : ''}</article>`;
  }).join('');
  return `<section class="panel learning-panel" id="learning-evolution" aria-labelledby="evolution-title"><div class="learning-panel-heading"><span class="learning-step">02</span><div><h2 id="evolution-title">掌握度与复习</h2><p>区分实际作答、后来纠错和当前有效证据。</p></div></div>${action('读取演化与复习', 'evolution', disabled)}${content}${report?.truncated ? '<p class="notice warning">历史较多，本页仅显示服务返回的最近记录。</p>' : ''}<p class="learning-caveat">证据均值与充分度不是掌握概率；失效版本的历史均值不作为当前分数。辅助、重复题族及已撤销观察不能延后独立复习时钟。</p></section>`;
}

function comparisonSection(report, disabled, request) {
  const scenarios = list(report?.scenarios);
  const form = `<form id="learning-comparison-form" class="learning-comparison-form"><div class="field-row"><div class="form-field"><label for="learning-budgets">每日预算（分钟）</label><input id="learning-budgets" name="budgets" value="${esc((request?.budgets_minutes_per_day || [20, 40]).join(','))}" required ${disabled ? 'disabled' : ''} aria-describedby="learning-budget-help"><small id="learning-budget-help">2—5 档预算，用逗号分隔；每档 1—480 分钟。</small></div><div class="form-field"><label for="learning-horizon">比较窗口（天）</label><input id="learning-horizon" name="horizon" type="number" min="1" max="30" step="1" value="${esc(request?.horizon_days ?? 7)}" required ${disabled ? 'disabled' : ''}></div></div><button class="button button-secondary" type="submit" ${disabled ? 'disabled' : ''}>比较时间预算</button><p class="form-error" data-learning-error hidden role="alert"></p></form>`;
  const content = !report ? empty('相同学习快照下比较可行目标、暂缓主题与前置约束，不会替换正式计划。') : !scenarios.length ? empty('暂无可比较方案，请检查学习范围和可用知识主题。') : `<div class="learning-scenarios">${scenarios.map(scenario => {
    const summary = scenario.summary || {};
    return `<article class="learning-scenario"><h3>每天 ${number(scenario.minutes_per_day)} 分钟</h3><dl class="learning-metrics">${metric('入选主题', number(summary.selected_topic_count))}${metric('主题覆盖', percent(summary.coverage_fraction))}${metric('估计用时', `${number(summary.estimated_minutes)} 分钟`)}${metric('到期 / 薄弱覆盖', `${number(summary.due_topics_covered)} / ${number(summary.weak_topics_covered)}`)}</dl>${list(scenario.schedule).length ? `<ol class="learning-schedule">${scenario.schedule.map(task => `<li><strong>${esc(task.name || task.topic_id)}</strong><span>${esc(task.scheduled_date)} · ${number(task.estimated_minutes)} 分钟</span></li>`).join('')}</ol>` : empty('当前约束下暂无可安排的任务。')}<details class="learning-details"><summary>暂缓 ${number(list(scenario.deferred).length)} · 受阻 ${number(list(scenario.blocked).length)}</summary>${[...list(scenario.blocked), ...list(scenario.deferred)].map(item => `<p>${esc(item.name || item.topic_id)}：${list(item.reason_codes).map(reason).join('、')}</p>`).join('') || '<p>没有受阻或暂缓主题。</p>'}</details></article>`;
  }).join('')}</div>`;
  return `<section class="panel learning-panel" id="learning-comparison" aria-labelledby="comparison-title"><div class="learning-panel-heading"><span class="learning-step">03</span><div><h2 id="comparison-title">时间预算比较</h2><p>看清有限时间内，哪些目标可以先完成。</p></div></div>${form}${content}<p class="learning-caveat">这是遵守前置、每日与每周预算、截止日期的确定性启发式预览；不保证全局最优，也不承诺学习收益。</p></section>`;
}

function replaySection(report, disabled) {
  const prediction = report?.prediction || {}, retests = report?.observed_retests || {};
  const policies = list(report?.policies);
  const names = { fixed_order: '固定顺序', existing_rules_topic_proxy: '原规则主题代理', adaptive_topic_proxy: '自适应主题代理' };
  let content = !report ? empty('在真实历史测验创建时点回放主题选择；没有历史时明确显示数据不足。') : `<dl class="learning-metrics">${metric('可预测 / 有效观察', `${number(prediction.predicted_observations)} / ${number(prediction.eligible_observations)}`)}${metric('预测覆盖', percent(prediction.coverage_fraction))}${metric('平均绝对误差', number(prediction.mean_absolute_error))}${metric('延迟独立复测对', number(retests.pair_count))}</dl>`;
  if (report && !list(report.decisions).length) content += empty('暂无可回放的已完成历史测验。');
  if (policies.length) content += `<div class="learning-table-wrap"><table class="learning-table"><caption>主题选择代理指标</caption><thead><tr><th scope="col">策略</th><th scope="col">决策数</th><th scope="col">薄弱 / 未知</th><th scope="col">前置主题</th><th scope="col">区别固定顺序</th></tr></thead><tbody>${policies.map(policy => `<tr><th scope="row">${esc(names[policy.policy] || policy.policy)}</th><td>${number(policy.decision_count)}</td><td>${number(policy.weak_or_unknown_choices)}</td><td>${number(policy.prerequisite_choices)}</td><td>${number(policy.different_from_fixed)}</td></tr>`).join('')}</tbody></table></div>`;
  if (report) content += Number(retests.pair_count) > 0 ? `<p class="notice">观察到的连续独立延迟复测：平均后测分数 ${number(retests.mean_followup_score)}；平均分数变化 ${number(retests.mean_score_change)}。这些变化不能归因于某一策略。</p>` : empty('暂无间隔至少 24 小时、题族不同的连续独立复测对，不计算学习提升。辅助作答或重复题族会中断配对，中途独立练习会重新计算间隔。');
  return `<section class="panel learning-panel" id="learning-replay" aria-labelledby="replay-title"><div class="learning-panel-heading"><span class="learning-step">04</span><div><h2 id="replay-title">策略回放与评估</h2><p>还原当时可得证据，分开看选择、预测与复测。</p></div></div>${action('回放真实历史', 'replay', disabled)}${content}<p class="learning-caveat">回放比较的是主题选择代理指标，不模拟题池覆盖和逐题前置排查。历史无法观测未执行策略的反事实结果；分数变化不是因果提升，也不是经校准的掌握概率。</p></section>`;
}

export function renderLearning({ mode, space, state, spaceSelectHtml = '' }) {
  const heading = '<div class="page-heading"><div><h1>学习实验</h1><p>用真实证据，决定下一步怎样学。</p></div></div>';
  if (!space) return `${heading}<div class="empty-state"><h2>先选择一个学习空间</h2><p>导入资料并创建空间后，再开启诊断与学习实验。</p><a href="#/spaces" class="button button-primary">查看学习空间</a></div>`;
  if (mode !== 'live') return `${heading}<div class="notice warning">四项学习实验需要真实学习数据。示例模式不会生成示例诊断、虚构掌握曲线或学习收益。</div><section class="panel learning-panel"><h2>连接你的真实学习空间</h2><p class="learning-empty">连接本地服务后可逐题诊断、查看复习演化、比较时间预算，并回放真实历史策略。</p><button type="button" class="button button-primary" data-action="settings">连接本地服务</button></section>`;
  const disabled = Boolean(state.busy);
  const comparison = comparisonSection(state.comparison, disabled, state.comparisonRequest);
  return `${heading}<div class="toolbar">${spaceSelectHtml}<span class="badge">${esc(space.name)} · 真实学习数据</span></div><nav class="learning-jumps" aria-label="学习实验内容"><a href="#learning-diagnostic" data-learning-jump="learning-diagnostic">自适应诊断</a><a href="#learning-evolution" data-learning-jump="learning-evolution">掌握与复习</a><a href="#learning-comparison" data-learning-jump="learning-comparison">预算比较</a><a href="#learning-replay" data-learning-jump="learning-replay">策略回放</a></nav><div id="learning-feedback" aria-live="polite">${state.busy ? `<p class="notice" role="status">${esc(state.busy)}</p>` : ''}${state.error ? `<p class="notice error" role="alert">${esc(state.error)}</p>` : ''}</div><div class="learning-sections" aria-busy="${disabled}">${diagnosticSection(state, disabled)}${evolutionSection(state.evolution, disabled)}${comparison}${replaySection(state.replay, disabled)}</div>`;
}
