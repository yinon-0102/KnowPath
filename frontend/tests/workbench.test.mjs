import test from 'node:test';
import assert from 'node:assert/strict';
import * as workbench from '../src/workbench.js';
const { parseRoute, spacePath, routeState, nextLearningStep, createWorkbenchController } = workbench;

test('multiple recoverable rounds lead to a choice and a free round remains reachable without a plan', () => {
 const space = { id: 's', topic_ids: ['topic'] };
 const plan = { id: 'p', tasks: [{ id: 't', status: 'pending' }] };
 const rows = ['a', 'b'].map(id => ({ id, space_id: 's', plan_id: 'p', task_id: 't', status: 'in_progress' }));
 const step = nextLearningStep(space, { current_plan: plan, recoverable_assessments: rows });
 assert.equal(step.kind, 'assessment'); assert.equal(step.assessment.id, undefined);
 assert.equal(step.assessment.task_id, 't');
 const free = nextLearningStep(space, { recoverable_assessments: [{ id: 'free', space_id: 's', status: 'ready' }] });
 assert.equal(free.kind, 'assessment'); assert.equal(free.assessment.id, 'free');
});

test('study workspace has a refreshable node route and maps to learning view', () => {
  assert.equal(parseRoute('#/space/s1/study?task=t1&plan=p1').section, 'study');
  assert.deepEqual(routeState('study'), { tab: 'study', learningSection: 'diagnostic' });
});

test('space sections survive links, encoded ids, refresh and legacy tabs', () => {
  const id = '空间/一';
  assert.deepEqual(parseRoute(spacePath(id, 'plan')), { name: 'space', id, section: 'plan', query: {} });
  assert.equal(parseRoute('#/space/s1').section, 'overview');
  assert.equal(parseRoute('#/space/s1/tasks').section, 'plan');
  assert.equal(parseRoute('#/space/%broken/plan').id, '');
  assert.equal(parseRoute('#/manage/session/p1/t1').id, 'session/p1/t1');
  assert.equal(parseRoute('#/materials/graph/m%2F1').id, 'm/1');
  assert.deepEqual(routeState('review'), { tab: 'assessment', learningSection: 'evolution' });
});

test('next action respects lifecycle and never requires initial assessment', () => {
  const space = { id: 's1', topic_ids: ['t1'] };
  assert.equal(nextLearningStep(space, {}).kind, 'plan');
  assert.equal(nextLearningStep({ ...space, topic_ids: [] }, {}).kind, 'scope');
  assert.equal(nextLearningStep({ ...space, status: 'archived' }, {}).kind, 'archived');
  assert.equal(nextLearningStep(space, { current_plan: { status: 'needs_replan' } }).kind, 'replan');
  assert.equal(nextLearningStep(space, { active_session: { session_id: 'x' }, current_plan: { status: 'needs_replan' } }).kind, 'session');
  const plan = { status: 'ready', tasks: [{ id: 'done', status: 'completed' }, { id: 't2', status: 'pending' }] };
  assert.equal(nextLearningStep(space, { current_plan: plan }, [{ review_schedule: { due: true } }]).task.id, 't2');
  assert.equal(nextLearningStep(space, { current_plan: plan }).task.id, 't2');
  assert.equal(nextLearningStep(space, { current_plan: { ...plan, tasks: [{ status: 'skipped' }] } }).kind, 'summary');
});

test('continue resumes an unfinished assessment for the current task before replanning', () => {
  const space = { id: 's1', topic_ids: ['topic'] };
  const plan = { id: 'p1', status: 'needs_replan', tasks: [{ id: 't1', status: 'pending' }] };
  const assessment = { assessment_id: 'a1', space_id: 's1', plan_id: 'p1', task_id: 't1', status: 'in_progress' };
  assert.equal(nextLearningStep(space, { current_plan: plan, recoverable_assessments: [assessment] }).assessment.assessment_id, 'a1');
  assert.equal(nextLearningStep(space, { current_plan: plan, recoverable_assessments: [{ ...assessment, task_id: 'foreign' }] }).kind, 'replan');
});

const completionStorage = () => { const map = new Map(); return { getItem: k => map.get(k) ?? null, setItem: (k, value) => map.set(k, value) }; };
const completionTarget = { spaceId: 's1', planId: 'p1', taskId: 't1', expectedPlanVersion: 1, source: 'assessment', assessmentId: 'a1' };

const confirmedCompletion = { id: 't1', space_id: 's1', plan_id: 'p1', plan_version: 2, status: 'completed',
  context: { completion: { event_id: 'event-1', assessment_id: 'a1', source: 'assessment' } },
  completion_receipt: { event_id: 'event-1', assessment_id: 'a1', source: 'assessment' }, next_action: { kind: 'summary', plan_id: 'p1' } };

test('lost completion response is recovered by bounded read-only queries without another PATCH', async () => {
  let writes = 0; const queries = [], states = [];
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), delay: async () => {}, changed: () => states.push(controller.snapshot(completionTarget).status), api: {
    updateTask: async () => { writes++; throw Object.assign(new Error('结果未知'), { code: 'TIMEOUT' }); },
    taskCompletionStatus: async (plan, task, body, options) => { queries.push({ plan, task, body, key: options.key }); return queries.length === 1 ? { status: 'unknown' } : { status: 'confirmed', result: confirmedCompletion }; },
  } });
  assert.deepEqual(await controller.complete(completionTarget), confirmedCompletion);
  assert.equal(writes, 1); assert.equal(queries.length, 2); assert.deepEqual(queries[0], queries[1]);
  assert.ok(states.includes('saving')); assert.ok(states.includes('querying')); assert.equal(controller.snapshot(completionTarget).status, 'saved');
});

test('unknown result survives reload and explicit recovery never resubmits completion', async () => {
  let writes = 0, reads = 0; const storage = completionStorage(); const commands = [];
  const api = { updateTask: async (p, t, body, options) => { writes++; commands.push({ body, key: options.key }); throw new Error('结果未知'); },
    taskCompletionStatus: async (p, t, body, options) => { reads++; assert.deepEqual({ body, key: options.key }, commands[0]); return { status: 'unknown' }; } };
  let controller = workbench.createTaskCompletionController({ storage, api, delay: async () => {} });
  await assert.rejects(controller.complete(completionTarget), /结果未知/);
  assert.equal(reads, 3); assert.equal(controller.snapshot(completionTarget).status, 'unknown');
  controller = workbench.createTaskCompletionController({ storage: { ...storage }, api, delay: async () => {} });
  await assert.rejects(controller.recover({ ...completionTarget, expectedPlanVersion: 8 }), /结果未知/);
  assert.equal(writes, 1); assert.equal(reads, 6);
  await assert.rejects(controller.complete({ ...completionTarget, expectedPlanVersion: 8 }), /结果未知/);
  assert.equal(writes, 2); assert.deepEqual(commands[0], commands[1]);
});

test('saved navigation recovery is durable and refuses a superseded completion receipt', async () => {
  const storage = completionStorage(); let reads = 0;
  const api = { updateTask: async () => confirmedCompletion, taskCompletionStatus: async () => { reads++; return { status: 'superseded' }; } };
  let controller = workbench.createTaskCompletionController({ storage, api });
  await controller.complete(completionTarget); controller.markNavigationPending(completionTarget, new Error('导航失败'));
  controller = workbench.createTaskCompletionController({ storage: { ...storage }, api });
  assert.equal(controller.snapshot(completionTarget).status, 'navigation_pending');
  assert.match(controller.snapshot(completionTarget).error, /导航失败/);
  await assert.rejects(controller.recover(completionTarget), /撤销|变化|失效/);
  assert.equal(reads, 1); assert.equal(controller.snapshot(completionTarget).result, undefined);
  assert.equal(controller.snapshot(completionTarget).pending, undefined);
});

test('undo during a pending response cannot resurrect the old completion', async () => {
  let finish; const controller = workbench.createTaskCompletionController({ storage: completionStorage(), api: { updateTask: () => new Promise(resolve => { finish = resolve; }) } });
  const saving = controller.complete(completionTarget);
  controller.acknowledgeUndo(completionTarget, 3); finish(confirmedCompletion);
  await assert.rejects(saving, /撤销|变化|失效/);
  assert.equal(controller.snapshot(completionTarget).result, undefined);
  assert.equal(controller.snapshot(completionTarget).status, 'idle');
});

test('persisted recovery entries can be listed by exact space after page reload', async () => {
  const storage = completionStorage();
  const api = { updateTask: async () => confirmedCompletion };
  let controller = workbench.createTaskCompletionController({ storage, api });
  await controller.complete(completionTarget); controller.markNavigationPending(completionTarget);
  controller = workbench.createTaskCompletionController({ storage: { ...storage }, api });
  const rows = controller.list('s1');
  assert.equal(rows.length, 1); assert.deepEqual(rows[0].target, completionTarget);
  assert.equal(rows[0].entry.status, 'navigation_pending'); assert.deepEqual(controller.list('foreign'), []);
  controller.acknowledgeNavigation(completionTarget);
  assert.deepEqual(controller.list('s1'), []);
});

test('double clicks share a single completion and query operation', async () => {
  let release, writes = 0, reads = 0;
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), delay: async () => {}, api: {
    updateTask: () => { writes++; return new Promise((resolve, reject) => { release = () => reject(new Error('结果未知')); }); },
    taskCompletionStatus: async () => { reads++; return { status: 'confirmed', result: confirmedCompletion }; },
  } });
  const first = controller.complete(completionTarget), second = controller.complete(completionTarget);
  assert.equal(first, second); release(); await first;
  assert.equal(writes, 1); assert.equal(reads, 1);
});

test('a different assessment cannot recover the original pending completion through the error path', async () => {
  let confirmed = false, reads = 0;
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), delay: async () => {}, api: {
    updateTask: async () => { throw new Error('结果未知'); },
    taskCompletionStatus: async () => { reads++; return confirmed ? { status: 'confirmed', result: confirmedCompletion } : { status: 'unknown' }; },
  } });
  await assert.rejects(controller.complete(completionTarget), /结果未知/); confirmed = true;
  await assert.rejects(controller.complete({ ...completionTarget, assessmentId: 'a2' }), /原完成确认结果未知/);
  assert.equal(reads, 3); assert.ok(controller.snapshot(completionTarget).pending);
});

test('a repeated completion with a saved receipt verifies it is still current before returning it', async () => {
  let writes = 0;
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), api: {
    updateTask: async () => { writes++; return confirmedCompletion; }, taskCompletionStatus: async () => ({ status: 'superseded' }),
  } });
  await controller.complete(completionTarget);
  await assert.rejects(controller.complete(completionTarget), /撤销|变化|失效/);
  assert.equal(writes, 1); assert.equal(controller.snapshot(completionTarget).result, undefined);
});

test('completion retries after reload preserve the original body and request identity', async () => {
  assert.equal(typeof workbench.createTaskCompletionController, 'function');
  const storage = completionStorage(), calls = [];
  const api = { updateTask: async (plan, task, body, options) => { calls.push({ plan, task, body, key: options.key }); throw new Error('结果未知'); } };
  let controller = workbench.createTaskCompletionController({ api, storage });
  await assert.rejects(controller.complete(completionTarget), /结果未知/);
  controller = workbench.createTaskCompletionController({ api, storage: { ...storage } });
  await assert.rejects(controller.complete({ ...completionTarget, expectedPlanVersion: 9 }), /结果未知/);
  assert.deepEqual(calls[0], calls[1]);
  assert.deepEqual(calls[0].body, { expected_plan_version: 1, status: 'completed', source: 'assessment', assessment_id: 'a1' });
});

test('a version conflict refreshes the exact target and requires another explicit confirmation', async () => {
  assert.equal(typeof workbench.createTaskCompletionController, 'function');
  let writes = 0, reads = 0; const bodies = [];
  const latest = { id: 'p1', space_id: 's1', version: 2, status: 'ready', tasks: [{ id: 'other', status: 'pending' }, { id: 't1', status: 'pending' }] };
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), api: {
    plan: async () => { reads++; return latest; },
    updateTask: async (p, t, body) => { writes++; bodies.push({ t, body }); if (writes === 1) throw Object.assign(new Error('版本冲突'), { code: 'VERSION_CONFLICT' }); return { id: t, next_action: { kind: 'task', task_id: 'other', plan_id: p } }; },
  } });
  await assert.rejects(controller.complete(completionTarget), /再次确认/);
  assert.equal(writes, 1); assert.equal(reads, 1);
  await controller.complete(completionTarget);
  assert.equal(writes, 2); assert.equal(bodies[1].t, 't1'); assert.equal(bodies[1].body.expected_plan_version, 2);
});

test('recovering a failed conflict refresh never submits before a separate new confirmation', async () => {
  const storage = completionStorage(); let reads = 0, writes = 0;
  const api = { plan: async () => { reads++; if (reads === 1) throw new Error('刷新离线'); return { id: 'p1', space_id: 's1', version: 2, status: 'ready', tasks: [{ id: 't1', status: 'pending' }] }; },
    updateTask: async () => { writes++; if (writes === 1) throw Object.assign(new Error('版本冲突'), { code: 'VERSION_CONFLICT' }); return { id: 't1', plan_version: 3 }; } };
  let controller = workbench.createTaskCompletionController({ api, storage });
  await assert.rejects(controller.complete(completionTarget), /刷新失败/); assert.equal(writes, 1);
  controller = workbench.createTaskCompletionController({ api, storage: { ...storage } });
  await assert.rejects(controller.complete(completionTarget), /再次确认/);
  assert.equal(reads, 2); assert.equal(writes, 1); assert.equal(controller.snapshot(completionTarget).latestPlan.version, 2);
  await controller.complete(completionTarget); assert.equal(writes, 2);
});

test('acknowledged completion survives a failed progress refresh without another task PATCH', async () => {
  const storage = completionStorage(); let writes = 0;
  const api = { updateTask: async () => { writes++; return { id: 't1', plan_version: 2 }; } };
  const first = workbench.createTaskCompletionController({ api, storage });
  const result = await first.complete(completionTarget);
  const restored = workbench.createTaskCompletionController({ api, storage: { ...storage } });
  assert.deepEqual(await restored.complete(completionTarget), result); assert.equal(writes, 1);
});

test('an uncertain completion cannot silently change to a different assessment', async () => {
  let writes = 0;
  const controller = workbench.createTaskCompletionController({ storage: completionStorage(), api: { updateTask: async () => { writes++; throw new Error('结果未知'); } } });
  await assert.rejects(controller.complete(completionTarget), /结果未知/);
  await assert.rejects(controller.complete({ ...completionTarget, assessmentId: 'a2' }), /原完成确认结果未知/);
  assert.equal(writes, 1);
});

test('a server failure with an unknown commit keeps the original completion identity', async () => {
  const calls = [], storage = completionStorage();
  const api = { updateTask: async (plan, task, body, options) => { calls.push({ body, key: options.key }); throw Object.assign(new Error('提交结果未知'), { status: 503 }); } };
  await assert.rejects(workbench.createTaskCompletionController({ api, storage }).complete(completionTarget), /未知/);
  await assert.rejects(workbench.createTaskCompletionController({ api, storage }).complete({ ...completionTarget, expectedPlanVersion: 3 }), /未知/);
  assert.deepEqual(calls[0], calls[1]);
});

test('round summary keeps task choices, assessments, verification, mastery and review separate', () => {
  assert.equal(typeof workbench.renderRoundSummary, 'function');
  const html = workbench.renderRoundSummary({ id: 's1' }, { plan_id: 'p1', processed: true, all_completed: false,
    tasks: { total: 3, completed: 1, skipped: 1, deferred: 1, remaining: 0 }, assessments: { completed: 2, unverified: 1 }, mastery: { mastered: 0, total: 3 }, review: { due: 1 } });
  for (const text of ['本轮已处理', '任务完成', '跳过', '延期', '已完成测评', '待核验', '掌握', '复习']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /全部完成|全部已掌握/);
  assert.match(html, /学习笔记/);
});

test('workbench restores server plan and paginates without losing previous rounds', async () => {
  let calls = 0;
  const controller = createWorkbenchController({ api: {
    workbench: async id => ({ space_id: id, current_plan: { id: 'p1', version: 1 }, active_session: null }),
    progress: async (id, options) => { calls++; return { space_id: id, current_plan_id: 'p1', current_plan_version: 1, rounds: [{ plan_id: options.cursor ? 'p0' : 'p1', nodes: [] }], total_count: 4, next_cursor: options.cursor ? null : 'next' }; },
    learningState: async () => ({ items: [] }), knowledgeUpdates: async () => ({ available_updates: [] }),
  } });
  await controller.load('s1'); await controller.load('s1');
  assert.equal(calls, 1); assert.equal(controller.snapshot('s1').data.current_plan.id, 'p1');
  await controller.load('s1', { more: true });
  assert.deepEqual(controller.snapshot('s1').progress.rounds.map(r => r.plan_id), ['p1', 'p0']);
  controller.invalidate('s1'); await controller.load('s1'); assert.equal(calls, 3);
});

test('invalidated requests cannot overwrite newer workbench state', async () => {
  let finish;
  const api = { workbench: id => new Promise(resolve => { finish = () => resolve({ space_id: id, current_plan: { id: 'old' } }); }), progress: async () => ({ rounds: [] }) };
  const controller = createWorkbenchController({ api });
  const pending = controller.load('s1'); controller.invalidate('s1'); finish(); await pending;
  assert.equal(controller.snapshot('s1').data, null);
});

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function routeProjection(id = 's1', version = 1, completed = 0) {
  const tasks = Array.from({ length: 21 }, (_, index) => ({ id: `t${index}`, node_id: `n${index}`, sequence: index + 1, status: index < completed ? 'completed' : 'pending' }));
  return {
    data: { space_id: id, current_plan: { id: `p-${id}`, space_id: id, version, status: 'ready', tasks } },
    progress: { space_id: id, current_plan_id: `p-${id}`, current_plan_version: version, completed_count: completed, total_count: 21, rounds: [{ plan_id: `p-${id}`, nodes: tasks.map(task => ({ ...task, task_id: task.id, plan_id: `p-${id}` })) }] },
  };
}

test('invalidating one space preserves its successful route and leaves other spaces ready', async () => {
  const controller = createWorkbenchController({ api: { workbench: async id => routeProjection(id).data, progress: async id => routeProjection(id).progress } });
  await Promise.all([controller.load('s1'), controller.load('s2')]);
  const other = controller.snapshot('s2');
  controller.invalidate('s1');
  assert.equal(controller.snapshot('s1').data.current_plan.tasks.length, 21);
  assert.equal(controller.snapshot('s1').stale, true);
  assert.equal(controller.snapshot('s2'), other); assert.equal(other.status, 'ready'); assert.equal(other.stale, false);
});

test('force refresh retains 21 nodes during loading and after failure, then retries successfully', async () => {
  let next = routeProjection(), request;
  const controller = createWorkbenchController({ api: { workbench: () => request ? request.promise : Promise.resolve(next.data), progress: async () => next.progress } });
  assert.equal(controller.snapshot('s1').status, 'not_loaded');
  await controller.load('s1');
  request = deferred();
  const refreshing = controller.load('s1', { force: true });
  assert.equal(controller.snapshot('s1').status, 'loading');
  assert.equal(controller.snapshot('s1').data.current_plan.tasks.length, 21);
  assert.equal(controller.snapshot('s1').progress.total_count, 21);
  request.reject(new Error('刷新离线')); await refreshing;
  assert.equal(controller.snapshot('s1').status, 'error');
  assert.equal(controller.snapshot('s1').data.current_plan.tasks.length, 21);
  assert.equal(controller.snapshot('s1').progress.total_count, 21);
  assert.match(controller.snapshot('s1').error, /刷新离线/);
  request = null; next = routeProjection('s1', 2, 1); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'ready'); assert.equal(controller.snapshot('s1').progress.completed_count, 1);
});

test('concurrent consumers await the same workbench load before choosing a task', async () => {
  const request = deferred(); let calls = 0, secondSettled = false;
  const controller = createWorkbenchController({ api: { workbench: () => { calls++; return request.promise; }, progress: async () => routeProjection().progress } });
  const first = controller.load('s1'), second = controller.load('s1').then(() => { secondSettled = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(secondSettled, false); assert.equal(calls, 1);
  request.resolve(routeProjection().data); await Promise.all([first, second]);
  assert.equal(controller.snapshot('s1').data.current_plan.tasks.length, 21);
});

test('a late pre-save response cannot replace a newer completed plan or another space', async () => {
  let request = null, current = routeProjection();
  const controller = createWorkbenchController({ api: { workbench: id => id === 's1' && request ? request.promise : Promise.resolve(id === 's1' ? current.data : routeProjection(id).data), progress: async id => id === 's1' ? current.progress : routeProjection(id).progress } });
  await Promise.all([controller.load('s1'), controller.load('s2')]);
  request = deferred(); const oldRequest = controller.load('s1', { force: true });
  const old = request; request = null; current = routeProjection('s1', 2, 1);
  await controller.load('s1', { force: true });
  old.resolve(routeProjection().data); await oldRequest;
  assert.equal(controller.snapshot('s1').data.current_plan.version, 2);
  assert.equal(controller.snapshot('s1').progress.completed_count, 1);
  assert.equal(controller.snapshot('s2').data.current_plan.version, 1);
});

test('workbench rejects foreign plans, downgraded versions and inconsistent progress without replacing success', async () => {
  let next = routeProjection('s1', 2, 1);
  const controller = createWorkbenchController({ api: { workbench: async () => next.data, progress: async () => next.progress } });
  await controller.load('s1');
  const original = controller.snapshot('s1').data;
  for (const invalid of [
    { ...routeProjection('s1', 3, 2), data: { ...routeProjection('s1', 3, 2).data, current_plan: { ...routeProjection('s1', 3, 2).data.current_plan, space_id: 'foreign' } } },
    routeProjection('s1', 1, 0),
    { data: routeProjection('s1', 3, 2).data, progress: routeProjection('s1', 3, 1).progress },
    { data: routeProjection('s1', 3, 2).data, progress: { space_id: 's1', rounds: [], total_count: 0, completed_count: 0 } },
  ]) {
    next = invalid; await controller.load('s1', { force: true });
    assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').data, original);
    assert.equal(controller.snapshot('s1').progress.completed_count, 1);
  }
});

test('a committed plan version is a floor for later workbench refreshes', async () => {
  let next = routeProjection();
  const controller = createWorkbenchController({ api: { workbench: async () => next.data, progress: async () => next.progress } });
  await controller.load('s1'); controller.invalidate('s1', { planId: 'p-s1', planVersion: 3 });
  next = routeProjection('s1', 2, 1); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').data.current_plan.version, 1);
  next = routeProjection('s1', 3, 2); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'ready'); assert.equal(controller.snapshot('s1').data.current_plan.version, 3);
});

test('progress requires proof of the same current plan and version even without current round nodes', async () => {
  const projection = routeProjection('s1', 2);
  const matching = { ...projection.progress, current_plan_id: 'p-s1', current_plan_version: 2, rounds: [{ plan_id: 'historical', nodes: [] }] };
  let progress = matching;
  const controller = createWorkbenchController({ api: { workbench: async () => projection.data, progress: async () => progress } });
  await controller.load('s1'); assert.equal(controller.snapshot('s1').status, 'ready');
  const original = controller.snapshot('s1').progress;
  for (const invalid of [
    { ...matching, current_plan_id: 'old-plan' }, { ...matching, current_plan_version: 1 },
    { ...matching, current_plan_id: null, current_plan_version: null },
    Object.fromEntries(Object.entries(matching).filter(([key]) => !key.startsWith('current_plan_'))),
  ]) {
    progress = invalid; await controller.load('s1', { force: true });
    assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').stale, true);
    assert.equal(controller.snapshot('s1').progress, original);
  }
  progress = matching; await controller.load('s1'); assert.equal(controller.snapshot('s1').status, 'ready');
});

test('history pages with changed current-plan proof preserve the whole projection until full refresh', async () => {
  for (const change of [{ current_plan_id: 'new-plan' }, { current_plan_version: 2 }]) {
    const projection = routeProjection(), initial = { ...projection.progress, current_plan_id: 'p-s1', current_plan_version: 1, next_cursor: 'older' };
    let page = { ...initial, ...change, total_count: 50, rounds: [{ plan_id: 'historical', nodes: [] }], next_cursor: null }, reads = 0;
    const controller = createWorkbenchController({ api: { workbench: async () => { reads++; return projection.data; }, progress: async (_, options) => options.cursor ? page : initial } });
    await controller.load('s1'); const oldProgress = controller.snapshot('s1').progress, oldData = controller.snapshot('s1').data;
    await controller.load('s1', { more: true });
    assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').stale, true);
    assert.equal(controller.snapshot('s1').progress, oldProgress); assert.equal(controller.snapshot('s1').data, oldData);
    await controller.load('s1', { more: true }); assert.equal(reads, 2); assert.equal(controller.snapshot('s1').status, 'ready');
    page = { ...page, current_plan_id: 'p-s1', current_plan_version: 1, total_count: 21 };
    await controller.load('s1', { more: true });
    assert.equal(controller.snapshot('s1').status, 'ready'); assert.equal(controller.snapshot('s1').progress.rounds.at(-1).plan_id, 'historical');
  }
});

test('an empty workbench requires an explicitly empty progress proof', async () => {
  let progress = { space_id: 's1', current_plan_id: null, current_plan_version: null, rounds: [], total_count: 0 };
  const controller = createWorkbenchController({ api: { workbench: async () => ({ space_id: 's1', current_plan: null }), progress: async () => progress } });
  await controller.load('s1'); assert.equal(controller.snapshot('s1').status, 'ready_empty');
  progress = { ...progress, current_plan_id: 'old-plan', current_plan_version: 1 }; await controller.load('s1', { force: true });
  assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').progress.current_plan_id, null);
});

test('only a successful no-plan projection is ready_empty', async () => {
  let offline = true;
  const controller = createWorkbenchController({ api: { workbench: async id => { if (offline) throw new Error('离线'); return { space_id: id, current_plan: null }; }, progress: async id => ({ space_id: id, current_plan_id: null, current_plan_version: null, rounds: [], completed_count: 0, total_count: 0 }) } });
  await controller.load('s1'); assert.equal(controller.snapshot('s1').status, 'error');
  offline = false; await controller.load('s1'); assert.equal(controller.snapshot('s1').status, 'ready_empty');
});

test('an acknowledged round rejects older or unproven plan replacements and accepts proven later rounds', async () => {
  const projection = (id, created_at, version = 1) => {
    const result = routeProjection('s1', version);
    result.data.current_plan = { ...result.data.current_plan, id, created_at };
    result.progress.current_plan_id = id;
    result.progress.rounds[0].plan_id = id;
    result.progress.rounds[0].nodes.forEach(node => { node.plan_id = id; });
    return result;
  };
  const created = '2026-10-17T00:00:00Z'; let next = projection('p-new', created);
  const controller = createWorkbenchController({ api: { workbench: async () => next.data, progress: async () => next.progress } });
  await controller.load('s1'); controller.invalidate('s1', { planId: 'p-new', planVersion: 2 });
  for (const invalid of [projection('p-earlier', created), projection('p-unknown', undefined), projection('p-new', created, 1)]) {
    next = invalid; await controller.load('s1');
    assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').stale, true);
    assert.equal(controller.snapshot('s1').data.current_plan.id, 'p-new');
  }
  next = projection('p-next', created); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'ready'); assert.equal(controller.snapshot('s1').data.current_plan.id, 'p-next');
  next = projection('p-new', created, 2); await controller.load('s1', { force: true });
  assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').data.current_plan.id, 'p-next');
  next = projection('p-after', '2026-10-18T00:00:00Z'); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'ready'); assert.equal(controller.snapshot('s1').data.current_plan.id, 'p-after');
});

test('an acknowledged task cannot be replaced by a transient empty workbench response', async () => {
  let next = routeProjection();
  const controller = createWorkbenchController({ api: { workbench: async () => next.data, progress: async () => next.progress } });
  await controller.load('s1'); controller.invalidate('s1', { planId: 'p-s1', planVersion: 2 });
  next = { data: { space_id: 's1', current_plan: null }, progress: { space_id: 's1', rounds: [], total_count: 0, completed_count: 0 } };
  await controller.load('s1');
  assert.equal(controller.snapshot('s1').status, 'error'); assert.equal(controller.snapshot('s1').progress.total_count, 21);
});

test('loading history after invalidation first refreshes the whole projection', async () => {
  let next = routeProjection(), cursors = [];
  const controller = createWorkbenchController({ api: { workbench: async () => next.data, progress: async (id, options) => { cursors.push(options.cursor); return { ...next.progress, next_cursor: 'next' }; } } });
  await controller.load('s1'); controller.invalidate('s1'); next = routeProjection('s1', 2, 1);
  await controller.load('s1', { more: true });
  assert.equal(controller.snapshot('s1').data.current_plan.version, 2);
  assert.deepEqual(cursors, [undefined, undefined]);
});

test('task assessment validates context and selects retest for completed tasks', () => {
  assert.equal(typeof workbench.taskAssessmentContext, 'function');
  const plan = { id: 'p1', space_id: 's1', status: 'needs_replan', tasks: [{ id: 't1', status: 'completed', topic_ids: ['topic'] }] };
  const ctx = workbench.taskAssessmentContext(plan, 't1', { spaceId: 's1', sessionId: 'session' });
  assert.equal(ctx.kind, 'retest');
  assert.deepEqual(ctx.allowed_kinds, ['retest']);
  assert.equal(ctx.learning_session_id, 'session');
  assert.deepEqual(ctx.topic_ids, ['topic']);
  assert.throws(() => workbench.taskAssessmentContext(plan, 't1', { spaceId: 'foreign' }));
  assert.throws(() => workbench.taskAssessmentContext({ ...plan, status: 'superseded' }, 't1'));
  assert.throws(() => workbench.taskAssessmentContext({ ...plan, tasks: [{ id: 't1', status: 'pending' }] }, 't1'));
  assert.throws(() => workbench.taskAssessmentContext({ ...plan, status: 'ready', tasks: [{ id: 't1', status: 'completed', context: { historical: true } }] }, 't1'));
});

test('progress responses from a different space are rejected without overwriting valid history', async () => {
  let wrong = false;
  const controller = createWorkbenchController({ api: {
    workbench: async id => ({ space_id: id }),
    progress: async id => ({ space_id: wrong ? 'foreign' : id, current_plan_id: null, current_plan_version: null, rounds: [{ plan_id: wrong ? 'foreign' : 'p1' }], next_cursor: 'next' }),
  } });
  await controller.load('s1'); wrong = true;
  await controller.load('s1', { more: true });
  assert.deepEqual(controller.snapshot('s1').progress.rounds.map(r => r.plan_id), ['p1']);
  assert.match(controller.snapshot('s1').error, /空间/);
});

test('management route keeps encoded identifiers intact for segment decoding', async () => {
  const { workspaceRoute, workspacePath } = await import('../src/workspace.js');
  const parsed = parseRoute(workspacePath('session', 'plan/a', '任务/b'));
  assert.equal(parsed.name, 'manage');
  assert.deepEqual(workspaceRoute(parsed.id), { page: 'session', id: 'plan/a', item: '任务/b' });
  assert.deepEqual(workspaceRoute('task/%broken/good'), { page: 'task', id: '', item: 'good' });
});


test('published material lookup exposes an unavailable service instead of an empty library', async () => {
  assert.equal(typeof workbench.publishedMaterialIds, 'function');
  const materials = [{ id: 'good' }, { id: 'draft' }, { id: 'archive', status: 'archived' }];
  const ids = await workbench.publishedMaterialIds(materials, async id => id === 'good' ? [{ graph_version: 2 }] : [{ graph_version: null }]);
  assert.deepEqual([...ids], ['good']);
  await assert.rejects(workbench.publishedMaterialIds(materials, async () => { throw new Error('backend offline'); }), /backend offline/);
});


test('learning updates render every relocated learning page, including budget comparison', () => {
  assert.equal(typeof workbench.isLearningRoute, 'function');
  for (const hash of ['#/learning/comparison', '#/space/s1/assessment', '#/space/s1/review', '#/space/s1/analysis', '#/space/s1/plan?view=budget']) {
    assert.equal(workbench.isLearningRoute(parseRoute(hash)), true, hash);
  }
  for (const hash of ['#/space/s1/plan', '#/space/s1/overview', '#/space/s1/assistant', '#/manage/session/p1/t1', '#/materials']) {
    assert.equal(workbench.isLearningRoute(parseRoute(hash)), false, hash);
  }
});

test('undo accepts fewer completed nodes at newer version and keeps ACK floor against old count', async () => {
  let next=routeProjection('s1',2,1);
  const controller=createWorkbenchController({api:{workbench:async()=>next.data,progress:async()=>next.progress}});
  await controller.load('s1'); controller.invalidate('s1',{planId:'p-s1',planVersion:3});
  next=routeProjection('s1',3,0); await controller.load('s1');
  assert.equal(controller.snapshot('s1').status,'ready'); assert.equal(controller.snapshot('s1').progress.completed_count,0);
  assert.equal(controller.snapshot('s1').progress.total_count,21);
  next=routeProjection('s1',2,1); await controller.load('s1',{force:true});
  assert.equal(controller.snapshot('s1').status,'error'); assert.equal(controller.snapshot('s1').data.current_plan.version,3);
  assert.equal(controller.snapshot('s1').progress.completed_count,0);
});

test('old undo receipt retains a newer completion acknowledgement', async () => {
  let writes=0;
  const controller=workbench.createTaskCompletionController({storage:completionStorage(),api:{updateTask:async()=>{writes++;return {id:'t1',plan_version:5,status:'completed'};}}});
  const target={...completionTarget,expectedPlanVersion:4};
  await controller.complete(target); controller.acknowledgeUndo(target,3);
  await controller.complete(target); assert.equal(writes,1);
});
