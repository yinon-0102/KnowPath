import { esc, storageRead, storageWrite } from './store.js';

const aliases = { tasks: 'plan', scope: 'materials', evolution: 'review', diagnostic: 'assessment', comparison: 'plan', replay: 'analysis' };
export const spaceSections = ['overview', 'study', 'summary', 'plan', 'materials', 'assistant', 'assessment', 'review', 'settings', 'analysis', 'notes'];
export function spacePath(id, section = 'overview', query = {}) {
  const params = new URLSearchParams(Object.entries(query).filter(([, value]) => value != null && value !== ''));
  return `#/space/${encodeURIComponent(id)}/${aliases[section] || section}${params.size ? '?' + params : ''}`;
}
export function parseRoute(hash) {
  const [path, search = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  const parts = path.split('/'), query = Object.fromEntries(new URLSearchParams(search));
  try {
    if (parts[0] === 'space') {
      const section = aliases[parts[2]] || parts[2] || 'overview';
      return { name: 'space', id: decodeURIComponent(parts[1] || ''), section: spaceSections.includes(section) ? section : 'overview', query };
    }
    if (parts[0] === 'manage') return { name: 'manage', id: parts.slice(1).join('/') };
    if (parts[0] === 'materials' && parts[1] === 'graph') return { name: 'material-graph', id: decodeURIComponent(parts.slice(2).join('/')) };
    return { name: parts[0] || 'overview', id: decodeURIComponent(parts.slice(1).join('/')) };
  } catch { return { name: parts[0] || 'overview', id: '', section: 'overview', query }; }
}
export function routeState(section) {
  if (section === 'study') return { tab: 'study', learningSection: 'diagnostic' };
  if (section === 'review') return { tab: 'assessment', learningSection: 'evolution' };
  if (section === 'analysis') return { tab: 'assessment', learningSection: 'replay' };
  if (section === 'plan') return { tab: 'tasks', learningSection: 'comparison' };
  return { tab: section === 'materials' ? 'scope' : section, learningSection: 'diagnostic' };
}
export function isLearningRoute(route) {
  return route?.name === 'learning' || route?.name === 'space' && (['assessment', 'review', 'analysis'].includes(route.section) || route.section === 'plan' && route.query?.view === 'budget');
}
export function taskAssessmentContext(plan, taskId, { spaceId = plan?.space_id, sessionId } = {}) {
  const task = plan?.tasks?.find(task => task.id === taskId);
  if (!task || plan.space_id !== spaceId) throw new Error('未找到当前空间中的测评任务。');
  if (['superseded', 'archived', 'cancelled', 'failed'].includes(plan.status) || task.historical || task.context?.historical || ['skipped', 'deferred'].includes(task.status)) throw new Error('此任务只能查看历史记录，不能创建关联测评。');
  if (plan.status !== 'ready' && task.status !== 'completed') throw new Error('请先重新规划，再开始任务测评。');
  return { space_id: spaceId, plan_id: plan.id || plan.plan_id, task_id: taskId, topic_ids: [...(task.topic_ids || [])],
    kind: task.status === 'completed' ? 'retest' : 'practice',
    ...(task.status === 'completed' ? { allowed_kinds: ['retest'] } : {}),
    ...(sessionId ? { learning_session_id: sessionId } : {}),
  };
}
export function nextLearningStep(space, workbench = {}, reviewItems = []) {
  if (space.status === 'archived') return { kind: 'archived', label: '恢复学习空间', description: '恢复后继续原有学习记录。' };
  if (!space.topic_ids?.length) return { kind: 'scope', label: '选择学习范围', description: '选择要学习的主题，随后可以直接生成计划，也可以先做初始测评。' };
  if (workbench.active_session) return { kind: 'session', label: '继续本次学习', session: workbench.active_session, description: workbench.current_plan?.status === 'needs_replan' ? '学习计划已变化，先结束原会话，再重新规划。' : '返回进行中的学习任务，接着上次的进度。' };
  const plan = workbench.current_plan;
  const recoverable = (workbench.recoverable_assessments || []).filter(item => (!item.space_id || item.space_id === space.id) && ['generating', 'ready', 'in_progress'].includes(item.status));
  const recovery = rows => rows.length === 1 ? rows[0] : { plan_id: rows.every(row => row.plan_id === rows[0].plan_id) ? rows[0].plan_id : undefined,
    task_id: rows.every(row => row.task_id === rows[0].task_id) ? rows[0].task_id : undefined };
  if (!plan && recoverable.length) return { kind: 'assessment', label: '继续测评', assessment: recovery(recoverable), description: '选择并恢复尚未完成的测评。' };
  if (!plan) return { kind: 'plan', label: '生成学习计划', description: '依据目标和当前范围安排下一轮任务，初始测评可选。' };
  const tasks = (plan.tasks || []).filter(task => !task.historical && !task.context?.historical);
  const assessments = tasks.flatMap(task => ['pending', 'in_progress'].includes(task.status)
    ? recoverable.filter(item => item.plan_id === (plan.id || plan.plan_id) && item.task_id === task.id) : []);
  if (assessments.length) return { kind: 'assessment', label: '继续本节点测评', assessment: recovery(assessments), plan, description: '恢复本节点尚未完成的同一轮测评。' };
  const free = recoverable.filter(item => !item.plan_id && !item.task_id);
  if (free.length) return { kind: 'assessment', label: '继续测评', assessment: recovery(free), plan, description: '选择并恢复尚未完成的测评。' };
  if (plan.status === 'needs_replan') return { kind: 'replan', label: '重新规划', description: '学习范围、资料或测评状态已变化，需要确认新的任务安排。' };
  const task = tasks.find(t => ['pending', 'in_progress'].includes(t.status));
  const reviewDue = reviewItems.some(item => item.current?.review_schedule?.due || item.review_schedule?.due || item.review_due || item.status === 'needs_review');
  return task ? { kind: 'task', label: '开始下一项任务', task, plan, reviewDue, description: '打开任务、阅读来源并记录本次学习。' }
    : { kind: 'summary', label: '查看本轮总结', plan, reviewDue, description: '本轮任务已处理，可核对完成、跳过、延期、测评与复习记录。' };
}

const COMPLETIONS = 'knowpath-task-completions-v1';
const completionStates = new WeakMap();
export function createTaskCompletionController({ api, storage, changed = () => {}, refreshed = async () => {}, delay = ms => new Promise(resolve => setTimeout(resolve, ms)), queryAttempts = 3 }) {
  // All entry points in a browser session share the same immutable command.
  let shared = storage && completionStates.get(storage);
  if (!shared) {
    shared = { entries: new Map(), inflight: new Map() };
    if (storage) completionStates.set(storage, shared);
  }
  const { entries, inflight } = shared;
  const identity = target => JSON.stringify([target.spaceId, target.planId, target.taskId]);
  const intent = value => JSON.stringify([value.source || 'manual', value.assessmentId || value.assessment_id || '', value.note || '', value.reason || '']);
  function snapshot(target) {
    const id = identity(target);
    if (!entries.has(id)) {
      const durable = storageRead(storage, COMPLETIONS, {})[id] || {};
      // A page reload interrupts the connection, not the underlying command.
      const status = ['saving', 'querying'].includes(durable.status) ? durable.result ? 'navigation_pending' : 'unknown' : durable.status || (durable.result ? 'saved' : durable.pending ? 'unknown' : 'idle');
      entries.set(id, { ...durable, status, busy: false });
    }
    return entries.get(id);
  }
  function persist(target, entry) {
    const { busy, ...durable } = entry;
    storageWrite(storage, COMPLETIONS, { ...storageRead(storage, COMPLETIONS, {}), [identity(target)]: durable });
  }
  const invalidated = () => Object.assign(new Error('原完成确认已撤销或发生变化，请核对当前任务后继续。'), { code: 'COMPLETION_SUPERSEDED' });
  function checkGeneration(entry, generation) { if ((entry.generation || 0) !== generation) throw invalidated(); }
  function setStatus(target, entry, status) {
    entry.status = status; entry.navigationPending = status === 'navigation_pending' || status === 'saved'; persist(target, entry); changed();
  }
  function acceptResult(target, entry, request, result) {
    delete entry.pending; delete entry.latestPlan;
    entry.result = result; entry.receiptRequest = request; entry.resultIntent = intent(request.body);
    entry.needsConfirmation = false; entry.error = ''; setStatus(target, entry, 'saved');
    return result;
  }
  async function queryResult(target, entry, request, generation) {
    if (!api.taskCompletionStatus) return null;
    setStatus(target, entry, 'querying');
    let lastError;
    for (let attempt = 0; attempt < Math.min(3, Math.max(1, queryAttempts)); attempt++) {
      if (attempt) await delay(attempt * 1000);
      checkGeneration(entry, generation);
      try {
        const response = await api.taskCompletionStatus(target.planId, target.taskId, { ...request.body }, { key: request.key, timeout: 6000 });
        checkGeneration(entry, generation);
        if (response.status === 'superseded') {
          delete entry.pending; delete entry.result; delete entry.receiptRequest; delete entry.resultIntent; delete entry.latestPlan;
          entry.needsConfirmation = true; entry.error = '原完成回执已撤销或失效，请刷新原任务并重新核对。';
          setStatus(target, entry, 'idle'); throw invalidated();
        }
        if (response.status === 'confirmed') {
          const result = response.result, receipt = result?.completion_receipt;
          const completion = result?.context?.completion || result?.task?.context?.completion;
          if (result?.space_id !== target.spaceId || result?.plan_id !== target.planId || (result?.id || result?.task?.id) !== target.taskId
            || !Number.isInteger(result.plan_version) || !receipt?.event_id || completion?.event_id !== receipt.event_id
            || receipt.source !== request.body.source || (receipt.assessment_id || '') !== (request.body.assessment_id || '')) {
            throw new Error('完成结果的任务或回执标识不一致，结果未知，请再次查询保存结果。');
          }
          return acceptResult(target, entry, request, result);
        }
        if (response.status !== 'unknown') throw new Error('完成结果返回格式无效，结果未知，请再次查询保存结果。');
      } catch (error) {
        checkGeneration(entry, generation);
        if (error.code === 'COMPLETION_SUPERSEDED') throw error;
        lastError = error;
      }
    }
    entry.error = `完成确认结果未知，原请求已保留。请查询保存结果，或明确重试原请求。${lastError ? `（${lastError.message}）` : ''}`;
    setStatus(target, entry, entry.result ? 'navigation_pending' : 'unknown');
    return null;
  }
  async function refreshTarget(target, entry) {
    const plan = await api.plan(target.planId);
    const task = plan.tasks?.find(item => item.id === target.taskId);
    entry.latestPlan = plan;
    entry.blocked = plan.space_id !== target.spaceId || (plan.id || plan.plan_id) !== target.planId
      || !['ready', 'needs_replan'].includes(plan.status) || !task || task.historical || task.context?.historical || !['pending', 'in_progress'].includes(task.status);
    persist(target, entry); await refreshed(plan, target);
  }
  function complete(target) {
    const id = identity(target);
    const entry = snapshot(target);
    const source = target.source || 'manual';
    const mismatch = entry.pending && (entry.pending.body.source !== source || (entry.pending.body.assessment_id || '') !== (target.assessmentId || ''));
    const unknown = () => new Error(entry.pending?.body.source === 'manual'
      ? '原完成确认结果未知，请先返回任务管理页恢复原手工完成确认，原备注将随同一请求保留。'
      : '原完成确认结果未知，请先恢复原任务与测评的确认请求。');
    if (inflight.has(id)) return mismatch ? Promise.reject(unknown()) : inflight.get(id);
    const operation = (async () => {
      const generation = entry.generation || 0;
      entry.busy = true; entry.error = ''; changed();
      try {
        if (mismatch) throw unknown();
        if (!entry.pending && entry.result && entry.resultIntent === intent(target) && target.expectedPlanVersion <= entry.result.plan_version) {
          if (!api.taskCompletionStatus) return entry.result;
          if (!entry.receiptRequest) throw new Error('原完成回执缺少请求标识，请核对当前任务记录。');
          const result = await queryResult(target, entry, entry.receiptRequest, generation);
          if (!result) throw new Error(entry.error);
          return result;
        }
        if (entry.needsConfirmation && !entry.latestPlan) {
          await refreshTarget(target, entry);
          throw new Error(entry.blocked ? '原任务已刷新，当前已处理或不再可确认，请核对学习路线。' : '原任务已刷新，请核对后再次确认完成。');
        }
        if (entry.blocked) throw new Error('原任务已完成、转为历史或不再可确认，请核对当前学习计划。');
        if (entry.pending && api.taskCompletionStatus) {
          const recovered = await queryResult(target, entry, entry.pending, generation);
          if (recovered) return recovered;
        }
        if (!entry.pending) {
          const version = entry.needsConfirmation ? entry.latestPlan.version : target.expectedPlanVersion;
          if (!Number.isInteger(version) || version < 1 || !target.spaceId || !target.planId || !target.taskId || source === 'assessment' && !target.assessmentId) throw new Error('完成确认缺少有效的任务、测评或计划版本。');
          entry.pending = { key: globalThis.crypto.randomUUID(), body: { expected_plan_version: version, status: 'completed', source,
            ...(source === 'assessment' ? { assessment_id: target.assessmentId } : {}),
            ...(target.note ? { note: target.note } : {}), ...(target.reason ? { reason: target.reason } : {}) } };
          entry.needsConfirmation = false; persist(target, entry);
        }
        const pending = entry.pending;
        setStatus(target, entry, 'saving');
        const result = await api.updateTask(target.planId, target.taskId, { ...pending.body }, { key: pending.key });
        checkGeneration(entry, generation);
        return acceptResult(target, entry, pending, result);
      } catch (error) {
        checkGeneration(entry, generation);
        if (mismatch) throw error;
        if (error.code === 'COMPLETION_SUPERSEDED') throw error;
        if (error.code === 'VERSION_CONFLICT') {
          delete entry.pending; delete entry.latestPlan; entry.needsConfirmation = true; setStatus(target, entry, 'idle');
          try { await refreshTarget(target, entry); }
          catch (refreshError) { entry.error = `计划版本已变化，刷新失败：${refreshError.message}。请再次确认前刷新原任务。`; throw Object.assign(new Error(entry.error), { code: 'VERSION_CONFLICT' }); }
          entry.error = entry.blocked ? '计划已更新，原任务已处理或不再可确认，请核对学习路线。' : '计划已更新，请核对原任务后再次确认完成。';
          throw Object.assign(new Error(entry.error), { code: 'VERSION_CONFLICT' });
        }
        if ([400, 401, 403, 404, 409, 410, 422].includes(error.status)) { delete entry.pending; setStatus(target, entry, 'idle'); }
        else if (entry.pending) {
          const recovered = await queryResult(target, entry, entry.pending, generation);
          if (recovered) return recovered;
          if (!api.taskCompletionStatus) setStatus(target, entry, 'unknown');
        }
        entry.error ||= error.message; persist(target, entry);
        throw Object.assign(new Error(entry.error), { code: error.code, status: error.status });
      } finally { entry.busy = false; changed(); }
    })();
    inflight.set(id, operation);
    operation.finally(() => inflight.delete(id)).catch(() => {});
    return operation;
  }
  function recover(target) {
    const id = identity(target), entry = snapshot(target);
    if (inflight.has(id)) return inflight.get(id);
    const operation = (async () => {
      entry.busy = true; changed();
      try {
        const request = entry.pending || entry.receiptRequest;
        if (!request) throw new Error('没有可查询的原完成请求，请核对当前任务。');
        if (request.body.source !== (target.source || 'manual') || (request.body.assessment_id || '') !== (target.assessmentId || '')) throw new Error('请恢复原任务与测评的完成确认请求。');
        const result = await queryResult(target, entry, request, entry.generation || 0);
        if (!result) throw new Error(entry.error || '完成确认结果未知，请稍后查询保存结果。');
        return result;
      } finally { entry.busy = false; changed(); }
    })();
    inflight.set(id, operation); operation.finally(() => inflight.delete(id)).catch(() => {}); return operation;
  }
  function markNavigationPending(target, error) {
    const entry = snapshot(target); if (!entry.result) return;
    entry.error = error?.message || String(error || '任务完成已保存，请点击继续学习恢复导航。');
    setStatus(target, entry, 'navigation_pending');
  }
  function acknowledgeNavigation(target) {
    const entry = snapshot(target); if (!entry.result) return;
    entry.error = ''; entry.status = 'saved'; entry.navigationPending = false; persist(target, entry); changed();
  }
  function list(spaceId) {
    const ids = new Set([...Object.keys(storageRead(storage, COMPLETIONS, {})), ...entries.keys()]);
    const rows = [];
    for (const id of ids) {
      let parts; try { parts = JSON.parse(id); } catch { continue; }
      if (!Array.isArray(parts) || parts.length !== 3 || parts[0] !== spaceId) continue;
      const target = { spaceId: parts[0], planId: parts[1], taskId: parts[2] }, entry = snapshot(target);
      if (!entry.pending && !entry.navigationPending) continue;
      const body = (entry.pending || entry.receiptRequest)?.body;
      if (!body) continue;
      Object.assign(target, { expectedPlanVersion: body.expected_plan_version, source: body.source || 'manual',
        ...(body.assessment_id ? { assessmentId: body.assessment_id } : {}), ...(body.note ? { note: body.note } : {}), ...(body.reason ? { reason: body.reason } : {}) });
      rows.push({ target, entry });
    }
    return rows;
  }
  function acknowledgeUndo(target, version) {
    const entry = snapshot(target);
    // The undo receipt certifies that any earlier completion intent has been
    // resolved and reversed. Later intents must still retain their own key.
    if (!Number.isInteger(version) || entry.pending?.body.expected_plan_version >= version || entry.result?.plan_version >= version) return;
    entry.generation = (entry.generation || 0) + 1;
    delete entry.pending; delete entry.result; delete entry.resultIntent;
    delete entry.receiptRequest;
    delete entry.latestPlan; delete entry.blocked; delete entry.needsConfirmation;
    entry.error = ''; setStatus(target, entry, 'idle');
  }
  return { complete, recover, snapshot, list, acknowledgeUndo, markNavigationPending, acknowledgeNavigation };
}

export function renderRoundSummary(space, summary) {
  const heading = '<h2>本轮总结</h2>';
  if (!summary) return `<section class="panel round-summary">${heading}<p role="status">正在读取本轮任务、测评与复习记录。</p><button class="button button-secondary" data-action="workbench-retry">刷新本轮总结</button></section>`;
  if (summary.details_pending) {
    const tasks = summary.tasks || {};
    return `<section class="panel round-summary">${heading}<p class="notice" role="status">${summary.all_completed ? '本轮全部任务已完成' : summary.processed ? '本轮已处理' : '节点完成已保存'}。</p><p>任务完成 ${Number(tasks.completed) || 0} / ${Number(tasks.total) || 0} · 待学习 ${Number(tasks.remaining) || 0}</p><p role="status">测评、掌握与复习统计正在刷新。</p><button class="button button-secondary" data-action="workbench-retry">刷新本轮总结</button><a class="text-link" href="${spacePath(space.id, 'plan')}">查看任务记录</a></section>`;
  }
  const count = value => Math.max(0, Math.trunc(Number(value) || 0));
  const tasks = summary.tasks || {}, assessments = summary.assessments || {}, mastery = summary.mastery || {}, review = summary.review || {};
  const status = summary.all_completed ? '本轮全部任务已完成' : summary.processed ? '本轮已处理' : '本轮仍有待学习任务';
  return `<section class="panel round-summary" aria-label="本轮总结">${heading}<p class="notice" role="status">${status}。任务完成与知识掌握分别记录。</p><dl class="workspace-fields"><div><dt>任务完成</dt><dd>${count(tasks.completed)} / ${count(tasks.total)}</dd></div><div><dt>跳过</dt><dd>${count(tasks.skipped)}</dd></div><div><dt>延期</dt><dd>${count(tasks.deferred)}</dd></div><div><dt>待学习</dt><dd>${count(tasks.remaining)}</dd></div><div><dt>已完成测评</dt><dd>${count(assessments.completed)} 轮</dd></div><div><dt>待核验</dt><dd>${count(assessments.unverified)} 题</dd></div><div><dt>当前确认掌握</dt><dd>${count(mastery.mastered)} / ${count(mastery.total)} 个主题</dd></div><div><dt>到期复习</dt><dd>${count(review.due)} 个主题</dd></div></dl><p class="subtle">跳过与延期均保留记录；测评高分、学习时长与任务完成都不会直接增加掌握事实。学习笔记独立保存和生成。</p><div class="learning-actions">${tasks.remaining ? `<a class="button button-primary" href="${spacePath(space.id, 'study')}">继续学习</a>` : '<button class="button button-primary" data-action="new-plan">安排下一轮</button>'}<a class="button button-secondary" href="${spacePath(space.id, 'review')}">掌握与复习</a><a class="button button-secondary" href="${spacePath(space.id, 'notes')}">学习笔记</a><a class="text-link" href="${spacePath(space.id, 'plan')}">查看任务记录</a></div>${summary.plan_id ? `<small class="subtle">本轮计划：${esc(summary.plan_id)}</small>` : ''}</section>`;
}

export function createWorkbenchController({ api, changed = () => {} }) {
  const entries = new Map(), epochs = new Map(), inflight = new Map(), versions = new Map(), committedPlans = new Map();
  const blank = () => ({ data: null, progress: null, review: null, updates: null, status: 'not_loaded', stale: true, busy: false, error: '', reviewError: '', updatesError: '' });
  function snapshot(id) { if (!entries.has(id)) entries.set(id, blank()); return entries.get(id); }
  function rememberVersion(id, planId, version) {
    if (!planId || !Number.isInteger(version)) return;
    const key = JSON.stringify([id, planId]);
    versions.set(key, Math.max(versions.get(key) || 0, version));
  }
  function laterPlan(plan, previous) {
    const time = Date.parse(plan?.created_at), oldTime = Date.parse(previous?.created_at);
    // Match the backend's current-round ordering: max(created_at, id).
    return Number.isFinite(time) && Number.isFinite(oldTime)
      && (time > oldTime || time === oldTime && (plan.id || plan.plan_id) > (previous.id || previous.plan_id));
  }
  function invalidate(id, { planId, planVersion } = {}) {
    const ids = id ? [id] : [...entries.keys()];
    for (const key of ids) {
      epochs.set(key, (epochs.get(key) || 0) + 1); inflight.delete(key);
      const entry = snapshot(key);
      entry.stale = true; entry.busy = false;
      if (entry.status === 'loading') entry.status = entry.data ? entry.data.current_plan ? 'ready' : 'ready_empty' : 'not_loaded';
      rememberVersion(key, planId, planVersion);
      if (planId && Number.isInteger(planVersion)) {
        const current = entry.data?.current_plan, prior = committedPlans.get(key);
        committedPlans.set(key, (current?.id || current?.plan_id) === planId ? current : (prior?.id || prior?.plan_id) === planId ? prior : { id: planId });
      }
    }
  }
  function validateData(id, data, previous) {
    if (data?.space_id !== id || data.current_plan?.space_id && data.current_plan.space_id !== id) throw new Error('工作台返回的空间标识不一致。');
    const plan = data.current_plan, old = previous?.current_plan;
    if (!plan && committedPlans.has(id)) throw new Error('已保存任务所属的计划尚未读取到，请重试刷新。');
    const planId = plan?.id || plan?.plan_id;
    const committed = committedPlans.get(id);
    if (plan && committed && planId !== (committed.id || committed.plan_id) && !laterPlan(plan, committed)) throw new Error('已保存任务所属的计划尚未读取到，或返回了较早的学习计划，请重试刷新。');
    if (plan && Number(plan.version || 0) < (versions.get(JSON.stringify([id, planId])) || 0)) throw new Error('计划版本已变化，读取到旧版本，请重试刷新。');
    if (plan && old && planId !== (old.id || old.plan_id) && !laterPlan(plan, old)) throw new Error('无法确认读取到更新的学习计划，请重试刷新。');
    const summary = data.round_summary;
    if (plan && summary && (summary.plan_id !== planId || summary.plan_version !== plan.version)) throw new Error('计划与本轮总结版本不一致，请重试刷新。');
  }
  function validateProgress(id, progress, data) {
    if (progress?.space_id !== id) throw new Error('学习进度返回的空间标识不一致。');
    const plan = data?.current_plan, planId = plan?.id || plan?.plan_id;
    // Every page certifies the same current-plan snapshot, including when its
    // canonical nodes live in an older round outside the returned page.
    if (!Object.hasOwn(progress, 'current_plan_id') || !Object.hasOwn(progress, 'current_plan_version')) throw new Error('学习进度缺少当前计划版本凭证，请更新后端并重试刷新。');
    if (progress.current_plan_id !== (planId || null) || progress.current_plan_version !== (plan?.version ?? null)) throw new Error('学习计划与进度的当前计划或版本不一致，请重试刷新。');
    if (!plan) return;
    const currentNodes = new Set((plan.tasks || []).filter(task => !task.historical && !task.context?.historical).map(task => task.node_id || task.context?.node_id || task.id || task.task_id));
    if (Number.isFinite(progress.total_count) && progress.total_count < currentNodes.size) throw new Error('学习计划与累计节点数量不一致，请重试刷新。');
    for (const round of progress.rounds || []) {
      if (round.plan_id === planId && round.plan_version != null && round.plan_version !== plan.version) throw new Error('学习计划与进度版本不一致，请重试刷新。');
      for (const node of round.nodes || []) {
        if (node.plan_id !== planId) continue;
        const task = plan.tasks?.find(task => (task.id || task.task_id) === node.task_id);
        if (task && node.status !== task.status && !(node.status === 'active' && ['pending', 'in_progress'].includes(task.status))) throw new Error('学习计划与进度状态不一致，请重试刷新。');
      }
    }
  }
  function load(id, { force = false, more = false } = {}) {
    if (force) invalidate(id);
    const entry = snapshot(id);
    if (more && entry.stale) more = false;
    if (inflight.has(id)) return inflight.get(id);
    if ((!more && entry.data && !entry.stale) || (more && !entry.progress?.next_cursor)) return Promise.resolve();
    const epoch = epochs.get(id) || 0;
    entry.busy = true; entry.status = 'loading'; entry.error = ''; changed(id);
    const valid = () => entries.get(id) === entry && (epochs.get(id) || 0) === epoch;
    const operation = (async () => { try {
      if (more) {
        const page = await api.progress(id, { cursor: entry.progress.next_cursor });
        validateProgress(id, page, entry.data);
        if (valid()) entry.progress = { ...page, rounds: [...entry.progress.rounds, ...page.rounds.filter(round => !entry.progress.rounds.some(r => r.plan_id === round.plan_id))] };
      } else {
        const results = await Promise.allSettled([api.workbench(id), api.progress(id, {}), api.learningState?.(id) || Promise.resolve({ items: [] }), api.knowledgeUpdates?.(id) || Promise.resolve({ available_updates: [] })]);
        if (!valid()) return;
        if (results[0].status === 'rejected') throw results[0].reason;
        const data = results[0].value;
        validateData(id, data, entry.data);
        if (results[1].status === 'rejected') throw results[1].reason;
        validateProgress(id, results[1].value, data);
        entry.data = data; entry.progress = results[1].value;
        rememberVersion(id, data.current_plan?.id || data.current_plan?.plan_id, data.current_plan?.version);
        if (committedPlans.get(id) && (committedPlans.get(id).id || committedPlans.get(id).plan_id) === (data.current_plan?.id || data.current_plan?.plan_id)) committedPlans.set(id, data.current_plan);
        entry.reviewError = entry.updatesError = '';
        if (results[2].status === 'fulfilled') {
          if (results[2].value.space_id && results[2].value.space_id !== id) entry.reviewError = '掌握状态返回的空间标识不一致。';
          else entry.review = results[2].value;
        } else entry.reviewError = results[2].reason.message;
        if (results[3].status === 'fulfilled') {
          if (results[3].value.space_id && results[3].value.space_id !== id) entry.updatesError = '知识更新返回的空间标识不一致。';
          else entry.updates = results[3].value;
        } else entry.updatesError = results[3].reason.message;
      }
      if (valid()) { entry.stale = false; entry.status = entry.data?.current_plan ? 'ready' : 'ready_empty'; }
    } catch (error) { if (valid()) { entry.error = error.message; entry.status = 'error'; entry.stale = true; } }
    finally { if (valid()) { entry.busy = false; inflight.delete(id); changed(id); } }
    })();
    inflight.set(id, operation);
    return operation;
  }
  return { snapshot, load, invalidate };
}


export async function publishedMaterialIds(materials, loadVersions) {
  const ids = await Promise.all(materials.filter(material => material.status !== 'archived').map(async material => {
    const versions = await loadVersions(material.id);
    return versions.some(version => Number(version.graph_version) > 0) ? material.id : null;
  }));
  return new Set(ids.filter(Boolean));
}
