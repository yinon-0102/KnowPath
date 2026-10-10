import test from 'node:test';
import assert from 'node:assert/strict';
import * as workbench from '../src/workbench.js';
const { parseRoute, spacePath, routeState, nextLearningStep, createWorkbenchController } = workbench;

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
  assert.equal(nextLearningStep(space, { current_plan: plan }, [{ review_schedule: { due: true } }]).kind, 'review');
  assert.equal(nextLearningStep(space, { current_plan: plan }).task.id, 't2');
  assert.equal(nextLearningStep(space, { current_plan: { ...plan, tasks: [{ status: 'skipped' }] } }).kind, 'finished');
});

test('workbench restores server plan and paginates without losing previous rounds', async () => {
  let calls = 0;
  const controller = createWorkbenchController({ api: {
    workbench: async id => ({ space_id: id, current_plan: { id: 'p1' }, active_session: null }),
    progress: async (id, options) => { calls++; return { space_id: id, rounds: [{ plan_id: options.cursor ? 'p0' : 'p1', nodes: [] }], total_count: 4, next_cursor: options.cursor ? null : 'next' }; },
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
    progress: async id => ({ space_id: wrong ? 'foreign' : id, rounds: [{ plan_id: wrong ? 'foreign' : 'p1' }], next_cursor: 'next' }),
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
