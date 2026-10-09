const aliases = { tasks: 'plan', scope: 'materials', evolution: 'review', diagnostic: 'assessment', comparison: 'plan', replay: 'analysis' };
export const spaceSections = ['overview', 'plan', 'materials', 'assistant', 'assessment', 'review', 'settings', 'analysis', 'notes'];
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
  if (!plan) return { kind: 'plan', label: '生成学习计划', description: '依据目标和当前范围安排下一轮任务，初始测评可选。' };
  if (plan.status === 'needs_replan') return { kind: 'replan', label: '重新规划', description: '学习范围、资料或测评状态已变化，需要确认新的任务安排。' };
  if (reviewItems.some(item => item.current?.review_schedule?.due || item.review_schedule?.due || item.review_due || item.status === 'needs_review')) return { kind: 'review', label: '查看到期复习', description: '用独立复测检查需要巩固的知识。' };
  const task = (plan.tasks || []).find(t => !t.context?.historical && ['pending', 'in_progress'].includes(t.status));
  return task ? { kind: 'task', label: '开始下一项任务', task, plan, description: '打开任务、阅读来源并记录本次学习。' } : { kind: 'finished', label: '查看测评与复习', description: '本轮任务已处理，可复测、查看掌握状态或安排下一轮。' };
}

export function createWorkbenchController({ api, changed = () => {} }) {
  const entries = new Map(), epochs = new Map();
  const blank = () => ({ data: null, progress: null, review: null, updates: null, busy: false, error: '', reviewError: '', updatesError: '' });
  function snapshot(id) { if (!entries.has(id)) entries.set(id, blank()); return entries.get(id); }
  function invalidate(id) {
    const ids = id ? [id] : [...entries.keys()];
    for (const key of ids) { epochs.set(key, (epochs.get(key) || 0) + 1); entries.delete(key); }
  }
  async function load(id, { force = false, more = false } = {}) {
    if (force) invalidate(id);
    const entry = snapshot(id);
    if (entry.busy || (!more && entry.data) || (more && !entry.progress?.next_cursor)) return;
    const epoch = epochs.get(id) || 0;
    entry.busy = true; entry.error = ''; changed(id);
    const valid = () => entries.get(id) === entry && (epochs.get(id) || 0) === epoch;
    try {
      if (more) {
        const page = await api.progress(id, { cursor: entry.progress.next_cursor });
        if (page.space_id !== id) throw new Error('学习进度返回的空间标识不一致。');
        if (valid()) entry.progress = { ...page, rounds: [...entry.progress.rounds, ...page.rounds.filter(round => !entry.progress.rounds.some(r => r.plan_id === round.plan_id))] };
      } else {
        const results = await Promise.allSettled([api.workbench(id), api.progress(id, {}), api.learningState?.(id) || Promise.resolve({ items: [] }), api.knowledgeUpdates?.(id) || Promise.resolve({ available_updates: [] })]);
        if (!valid()) return;
        if (results[0].status === 'rejected') throw results[0].reason;
        entry.data = results[0].value;
        if (entry.data.space_id !== id) throw new Error('工作台返回的空间标识不一致。');
        if (results[1].status === 'fulfilled' && results[1].value.space_id === id) entry.progress = results[1].value;
        else entry.error = results[1].status === 'rejected' ? results[1].reason.message : '学习进度返回的空间标识不一致。';
        if (results[2].status === 'fulfilled') entry.review = results[2].value; else entry.reviewError = results[2].reason.message;
        if (results[3].status === 'fulfilled') entry.updates = results[3].value; else entry.updatesError = results[3].reason.message;
      }
    } catch (error) { if (valid()) { entry.error = error.message; if (!entry.data?.space_id || entry.data.space_id !== id) entry.data = null; } }
    finally { if (valid()) { entry.busy = false; changed(id); } }
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
