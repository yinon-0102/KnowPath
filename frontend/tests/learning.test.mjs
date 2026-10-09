import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning, comparisonPayload, assessmentTopics, assessmentPayload } from '../src/learning.js';
import { createApi, ApiError } from '../src/api.js';

test('concurrent exposure exclusions and history limits are explained without leaking other questions', () => {
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: {
    busy: '', error: '', assessmentId: 'a1', diagnostic: {
      assessment_id: 'a1', status: 'ready', current_question: null,
      progress: { answered: 0, target: 5 }, completion_reason: 'exposure_history_limit',
      runtime_excluded_family_count: 3, runtime_family_exclusions: [{ question_id: 'HIDDEN_OTHER_QUESTION' }],
    },
  } });
  assert.match(html, /历史记录超出在线检查上限/);
  assert.match(html, /跳过 3 类/);
  assert.doesNotMatch(html, /HIDDEN_OTHER_QUESTION/);
});

const memory = () => { const map = new Map(); return { getItem: k => map.get(k) ?? null, setItem: (k, v) => map.set(k, v) }; };
const trace = (id = 'q1') => ({ assessment_id: 'a1', adaptive: true, status: 'in_progress', policy_version: 'adaptive-v1', current_question: id ? { id, type: 'single_choice', prompt: 'Which definition?', options: [{ id: 'A', text: 'Definition' }, { id: 'B', text: 'Alternative' }] } : null, progress: { answered: id === 'q1' ? 0 : id ? 1 : 2, presented: id === 'q1' ? 1 : 2, target: 5 }, decisions: [], hypotheses: [], completion_reason: id ? null : 'no_unseen_question_family' });
const context = () => ({ mode: 'live', spaceId: 's1', active: true });

test('assessment selection uses only available current-scope topics with sources', () => {
  const source = { material_id: 'm1', material_version_id: 'v1', chunk_id: 'c1' };
  const topics = [
    { id: 'included', source_refs: [source] }, { id: 'excluded', source_refs: [source] },
    { id: 'outside', source_refs: [source] }, { id: 'manual', automatic_questions: false, source_refs: [source] },
    { id: 'empty', source_refs: [] }, { id: 'archived', status: 'archived', source_refs: [source] },
  ];
  const eligible = assessmentTopics({ topic_ids: ['included', 'excluded', 'manual', 'empty', 'archived'], excluded_topic_ids: ['excluded'] }, topics);
  assert.deepEqual(eligible.map(t => t.id), ['included']);
  assert.deepEqual(assessmentPayload({ topic: 'included', count: '7', kind: 'practice', type: 'mixed' }, eligible), {
    adaptive: false, kind: 'practice', question_count: 7, topic_ids: ['included'], question_types: ['single_choice', 'short_answer'],
  });
  assert.throws(() => assessmentPayload({ topic: 'outside', count: 5, kind: 'diagnostic', type: 'single_choice' }, eligible), /学习范围/);
  assert.throws(() => assessmentPayload({ count: 5, kind: 'diagnostic', type: 'single_choice' }, []), /知识主题/);
});

test('knowledge setup reaches generation exactly and survives a page reload', async () => {
  const storage = memory(), calls = [];
  const topic = { id: 't1', name: 'Bound knowledge', source_refs: [{ chunk_id: 'c1' }] };
  const api = { space: async () => ({ id: 's1', topic_ids: ['t1'] }), topics: async () => [topic],
    createAssessment: async (id, body, options) => { calls.push({ id, body, key: options.key }); throw new ApiError('响应丢失', 0, 'NETWORK'); } };
  const setup = { topic: 't1', count: '8', type: 'mixed', kind: 'practice' };
  const controller = createLearningController({ api, context, storage });
  await controller.loadKnowledge(); controller.configure(setup); await controller.start(setup);
  const restored = createLearningController({ api, context, storage });
  await restored.loadKnowledge();
  assert.deepEqual(restored.snapshot().setup, setup);
  await restored.start({ ...setup, count: '5' });
  assert.equal(calls.length, 2); assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[0].body.question_count, 8); assert.deepEqual(calls[0].body.topic_ids, ['t1']);
});

test('learning routes render one functional panel at a time', () => {
  for (const section of ['diagnostic', 'evolution', 'comparison', 'replay']) {
    const html = renderLearning({ mode: 'live', section, space: { id: 's1', name: 'Space' }, state: {} });
    assert.equal(html.split('<section class="panel learning-panel"').length - 1, 1);
    assert.ok(html.includes(`id="learning-${section}"`));
    assert.ok(html.includes(`href="#/learning/${section}" aria-current="page"`));
  }
});

test('task assessments preserve their association and restrict questions to the task topic subset', () => {
  const setup = { count: '5', kind: 'retest', type: 'single_choice', topic_ids: ['t2'], plan_id: 'p1', task_id: 'task1', learning_session_id: 'ss1' };
  const body = assessmentPayload(setup, [{ id: 't1' }, { id: 't2' }]);
  assert.deepEqual(body.topic_ids, ['t2']);
  assert.equal(body.plan_id, 'p1'); assert.equal(body.task_id, 'task1'); assert.equal(body.learning_session_id, 'ss1');
  for (const patch of [{ topic_ids: ['outside'] }, { topic_ids: [] }, { topic_ids: ['t2', 't2'] }, { topic: 't1' }]) {
    assert.throws(() => assessmentPayload({ ...setup, ...patch }, [{ id: 't1' }, { id: 't2' }]), /主题|范围/);
  }
});

test('assessment creation reads the latest task association and drops a context from another space', async () => {
  let association = { space_id: 's1', plan_id: 'old', task_id: 'old-task', topic_ids: ['t1'] };
  const calls = [], storage = memory();
  const api = { space: async () => ({ id: 's1' }), topics: async () => [{ id: 't1', source_refs: [{}] }, { id: 't2', source_refs: [{}] }],
    createAssessment: async (spaceId, body) => { calls.push(body); throw new ApiError('未创建', 400, 'INVALID_REQUEST'); } };
  const controller = createLearningController({ api, storage, context: () => ({ ...context(), assessmentContext: association }) });
  await controller.loadKnowledge();
  controller.configure({ count: '5', kind: 'practice', type: 'single_choice' });
  association = { space_id: 's1', plan_id: 'p2', task_id: 'task2', learning_session_id: 'ss2', topic_ids: ['t2'] };
  await controller.start(controller.snapshot().setup);
  assert.equal(calls[0].plan_id, 'p2'); assert.equal(calls[0].task_id, 'task2'); assert.equal(calls[0].learning_session_id, 'ss2');
  assert.deepEqual(calls[0].topic_ids, ['t2']);
  // An explicit validation failure permits a fresh creation, unlike an ambiguous network response.
  const fresh = createLearningController({ api, storage: memory(), context: () => ({ ...context(), assessmentContext: { ...association, space_id: 's2' } }) });
  await fresh.loadKnowledge();
  await fresh.start({ count: '5', kind: 'practice', type: 'single_choice', plan_id: 'stale', task_id: 'stale', learning_session_id: 'stale', topic_ids: ['t2'] });
  assert.ok(!('plan_id' in calls[1])); assert.ok(!('task_id' in calls[1])); assert.ok(!('learning_session_id' in calls[1]));
  assert.deepEqual(calls[1].topic_ids, ['t1', 't2']);
});

test('embedded learning and hideNav omit jumps while review offers a retest only for due topics', () => {
  const state = { evolution: { items: [
    { topic_id: 'due"topic', topic_name: 'Due', current: { review_schedule: { due: true } } },
    { topic_id: 'later', topic_name: 'Later', current: { review_schedule: { due: false } } },
  ] } };
  for (const props of [{ embedded: true }, { hideNav: true }]) {
    const html = renderLearning({ mode: 'live', section: 'evolution', space: { id: 's1', name: 'Space' }, state, ...props });
    assert.doesNotMatch(html, /learning-jumps|id="learning-diagnostic"/);
    assert.equal((html.match(/data-action="review-topic"/g) || []).length, 1);
    assert.match(html, /data-topic="due&quot;topic"/);
  }
});

test('task context updates the setup scope and restricts the displayed topic choices', async () => {
  let association = { space_id: 's1', plan_id: 'p1', task_id: 'task1', topic_ids: ['t2'] };
  const controller = createLearningController({ api: { space: async () => ({ id: 's1' }), topics: async () => [
    { id: 't1', name: 'Other topic', source_refs: [{}] }, { id: 't2', name: 'Task topic', source_refs: [{}] },
  ] }, context: () => ({ ...context(), assessmentContext: association }), storage: memory() });
  await controller.loadKnowledge();
  controller.configure({ topic: 't1' });
  const state = controller.snapshot();
  assert.deepEqual(state.setup.topic_ids, ['t2']); assert.equal(state.setup.topic, '');
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state, embedded: true });
  assert.match(html, /当前任务主题（1）/); assert.match(html, /<option value="t2"/); assert.doesNotMatch(html, /<option value="t1"/);
  association = { ...association, space_id: 's2' };
  assert.ok(!('plan_id' in controller.snapshot().setup)); assert.ok(!('topic_ids' in controller.snapshot().setup));
});

test('ambiguous task assessment retries preserve the original association after navigation', async () => {
  let association = { space_id: 's1', plan_id: 'p1', task_id: 'task1', learning_session_id: 'ss1', topic_ids: ['t1'] };
  const calls = [];
  const controller = createLearningController({ api: { space: async () => ({ id: 's1' }), topics: async () => [{ id: 't1', source_refs: [{}] }, { id: 't2', source_refs: [{}] }],
    createAssessment: async (id, body, options) => { calls.push({ body, key: options.key }); throw new ApiError('响应丢失', 0, 'NETWORK'); },
  }, context: () => ({ ...context(), assessmentContext: association }), storage: memory() });
  await controller.loadKnowledge(); await controller.start({ count: '5', kind: 'practice', type: 'single_choice' });
  association = { ...association, plan_id: 'p2', task_id: 'task2', learning_session_id: 'ss2', topic_ids: ['t2'] };
  await controller.start({ count: '6', kind: 'practice', type: 'single_choice' });
  assert.deepEqual(calls[0], calls[1]); assert.equal(calls[0].body.plan_id, 'p1');
});

const failedRun = (runId = 'r1') => new ApiError('Generated questions failed validation.', 200,
  'QUESTION_VALIDATION_FAILED', { runId, runStatus: 'failed' });

test('failed generation unlocks an explicit new request and renders validated questions', async () => {
  const keys = [], storage = memory(); let creates = 0;
  const api = {
    createAssessment: async (spaceId, body, options) => { keys.push(options.key); creates++; return { assessment_id: 'a' + creates, run_id: 'r' + creates }; },
    waitRun: async id => { if (id === 'r1') throw failedRun(); },
    assessment: async () => ({ id: 'a2', space_id: 's1', adaptive: true, status: 'ready' }),
    diagnostic: async () => ({ ...trace(), assessment_id: 'a2' }),
  };
  const controller = createLearningController({ api, context, storage });
  await controller.start();
  assert.equal(creates, 1); // No automatic model job retry.
  assert.equal(controller.snapshot().assessmentStatus, 'failed');
  assert.match(controller.snapshot().error, /未通过校验/);
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: controller.snapshot() });
  assert.match(html, /id="learning-start-form"/);
  assert.ok(html.includes('>重新生成题目</button>'));
  assert.match(html, /QUESTION_VALIDATION_FAILED/);
  assert.equal(html.split('生成的题目未通过校验').length - 1, 1);
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /learning-answer-form|learning-finalize/);
  // A page reload retains the terminal state and the recovery action.
  const restored = createLearningController({ api, context, storage });
  assert.equal(restored.snapshot().assessmentStatus, 'failed');
  await restored.start();
  assert.equal(creates, 2); assert.notEqual(keys[0], keys[1]);
  assert.equal(restored.snapshot().diagnostic.current_question.id, 'q1');
  assert.equal(restored.snapshot().generationError, null);
  assert.equal(restored.snapshot().error, '');
});

test('resuming a saved generating assessment records terminal failure without requesting a diagnostic', async () => {
  const storage = memory(); let reads = 0, diagnostics = 0;
  storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s1: { assessmentId: 'a1', runId: 'r1' } }));
  const api = { assessment: async () => { reads++; return { id: 'a1', space_id: 's1', adaptive: true, status: reads === 1 ? 'generating' : 'failed', run_id: 'r1' }; },
    waitRun: async () => { throw failedRun(); }, diagnostic: async () => { diagnostics++; throw new Error('Not available'); } };
  const controller = createLearningController({ api, context, storage });
  await controller.resume(); await controller.resume();
  assert.equal(controller.snapshot().assessmentStatus, 'failed');
  assert.equal(controller.snapshot().generationError.code, 'QUESTION_VALIDATION_FAILED');
  assert.equal(diagnostics, 0);
});

test('a lost poll response does not enable duplicate creation and can resume the same assessment', async () => {
  let creates = 0, polls = 0;
  const storage = memory();
  const api = { createAssessment: async () => { creates++; return { assessment_id: 'a1', run_id: 'r1' }; },
    waitRun: async () => { polls++; if (polls === 1) throw new ApiError('连接中断', 0, 'NETWORK'); },
    assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: polls === 1 ? 'generating' : 'ready', run_id: 'r1' }),
    diagnostic: async () => trace() };
  const controller = createLearningController({ api, context, storage });
  await controller.start(); await controller.start();
  assert.equal(creates, 1); assert.equal(controller.snapshot().assessmentStatus, 'generating');
  const restored = createLearningController({ api, context, storage });
  await restored.resume();
  assert.equal(creates, 1); assert.equal(restored.snapshot().diagnostic.current_question.id, 'q1');
});

test('ambiguous regeneration retains one key across page reloads', async () => {
  const keys = [], storage = memory();
  const api = { createAssessment: async (spaceId, body, options) => { keys.push(options.key); if (keys.length > 1) throw new ApiError('连接中断', 0, 'NETWORK'); return { assessment_id: 'a1', run_id: 'r1' }; },
    waitRun: async () => { throw failedRun(); } };
  const controller = createLearningController({ api, context, storage });
  await controller.start(); await controller.start();
  await createLearningController({ api, context, storage }).start();
  assert.notEqual(keys[0], keys[1]); assert.equal(keys[1], keys[2]);
});

test('a late generation failure cannot unlock a different space or leak its error', async () => {
  let selected = 's1', reject, ready;
  const polling = new Promise(resolve => { ready = resolve; });
  const controller = createLearningController({ storage: memory(),
    context: () => ({ mode: 'live', spaceId: selected, active: true }), api: {
      createAssessment: async () => ({ assessment_id: 'a1', run_id: 'r1' }),
      waitRun: () => new Promise((_, fail) => { reject = fail; ready(); }),
    } });
  const pending = controller.start(); await polling;
  selected = 's2'; controller.sync(); reject(failedRun()); await pending;
  assert.equal(controller.snapshot().assessmentStatus, '');
  assert.equal(controller.snapshot().assessmentId, '');
  assert.equal(controller.snapshot().generationError, null);
});

test('learning API preserves exact request bodies, auth, URL encoding and retry keys', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, ...options }); return new Response('{}', { headers: { 'Content-Type': 'application/json' } }); });
  const api = createApi(() => 'local');
  await api.createAssessment('s/1', { adaptive: true, kind: 'diagnostic', question_count: 5, question_types: ['single_choice'] }, { key: 'create-key' });
  await api.diagnostic('a/1');
  await api.recordAttempt('a1', { answers: [{ question_id: 'q1', expected_answer_revision: 0, answer: 'A' }] }, { key: 'answer-key' });
  await api.finalizeAssessment('a1', { allow_unanswered: false });
  await api.evolution('s1');
  await api.comparePlans('s1', { budgets_minutes_per_day: [20, 40], horizon_days: 7 }, { key: 'comparison-key' });
  await api.replayPolicies('s1', {}, { key: 'replay-key' });
  assert.ok(calls[0].url.endsWith('learning-spaces/s%2F1/assessments'));
  assert.equal(calls[0].headers['Idempotency-Key'], 'create-key');
  assert.equal(calls[2].headers['Idempotency-Key'], 'answer-key');
  assert.ok(calls.every(c => c.headers['X-Local-Token'] === 'local'));
  assert.deepEqual(JSON.parse(calls[5].body), { budgets_minutes_per_day: [20, 40], horizon_days: 7 });
  assert.deepEqual(JSON.parse(calls[6].body), {});
  assert.ok(calls[1].url.endsWith('assessments/a%2F1/diagnostic'));
  assert.ok(calls[4].url.endsWith('learning-spaces/s1/evolution'));
});

test('adaptive controller starts, polls, answers only current question and finalizes', async () => {
  const calls = []; let step = 0;
  const api = { createAssessment: async (id, body) => { calls.push(body); return { assessment_id: 'a1', run_id: 'r1' }; },
    waitRun: async id => calls.push(id), assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: step === 3 ? 'completed' : 'ready' }),
    diagnostic: async () => step === 3 ? { ...trace(null), status: 'completed' } : trace(step === 0 ? 'q1' : step === 1 ? 'q2' : null),
    recordAttempt: async (id, body) => { calls.push(body); step++; return {}; },
    finalizeAssessment: async (id, body) => { calls.push(body); step = 3; return { run_id: 'final' }; },
    assessmentResult: async () => ({ question_results: [{ verdict: 'correct', score: 1 }] }) };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.start();
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'q1');
  assert.equal(calls[0].adaptive, true);
  assert.equal(calls[1], 'r1');
  await controller.answer('A');
  assert.deepEqual(calls[2], { answers: [{ question_id: 'q1', expected_answer_revision: 0, answer: 'A', elapsed_seconds: 0 }] });
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'q2');
  await controller.answer('B');
  await controller.finalize();
  assert.equal(controller.snapshot().diagnostic.status, 'completed');
  assert.equal(controller.snapshot().result.question_results.length, 1);
});

test('resume restores same assessment and validates the owning space', async () => {
  const storage = memory(); let creates = 0;
  const api = { createAssessment: async () => { creates++; return { assessment_id: 'a1', run_id: 'r1' }; }, waitRun: async () => {},
    assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }), diagnostic: async () => trace() };
  await createLearningController({ api, context, storage }).start();
  const restored = createLearningController({ api, context, storage });
  await restored.resume();
  assert.equal(creates, 1); assert.equal(restored.snapshot().diagnostic.current_question.id, 'q1');
  api.assessment = async () => ({ id: 'a1', space_id: 'other', status: 'ready' });
  await restored.resume();
  assert.match(restored.snapshot().error, /空间/);
  assert.equal(restored.snapshot().diagnostic, null);
});

test('late requests cannot leak results into another selected space', async () => {
  let selected = 's1', resolve;
  const controller = createLearningController({ api: { evolution: () => new Promise(r => { resolve = r; }) },
    context: () => ({ mode: 'live', spaceId: selected, active: true }), storage: memory() });
  const pending = controller.loadEvolution();
  selected = 's2'; controller.sync();
  resolve({ space_id: 's1', items: [{ topic_name: 'private space one' }] });
  await pending;
  assert.equal(controller.snapshot().spaceId, 's2');
  assert.equal(controller.snapshot().evolution, null);
  assert.equal(controller.snapshot().busy, '');
});

test('knowledge lookup always reads the current backend scope without a mode selector', async () => {
  const space = { id: 's1', topic_ids: ['t1'] };
  const topics = [{ id: 't1', title: '向量', source_refs: [{ material_id: 'm1' }] }];
  const calls = [];
  const controller = createLearningController({ api: {
    space: async id => { calls.push(['space', id]); return space; },
    topics: async selected => { calls.push(['topics', selected.id]); return topics; },
  }, context: () => ({ spaceId: 's1', active: true }), storage: memory() });
  await controller.loadKnowledge();
  assert.deepEqual(calls, [['space', 's1'], ['topics', 's1']]);
  assert.deepEqual(controller.snapshot().knowledge.eligible, topics);
  assert.equal(controller.snapshot().diagnostic, null);
});

test('renderer escapes backend values and excludes hidden question internals', () => {
  const attack = '<img src=x onerror=alert(1)>';
  const diagnostic = trace(); diagnostic.current_question.prompt = attack;
  diagnostic.current_question.answer_key = 'SECRET_KEY'; diagnostic.current_question.rubric = 'SECRET_RUBRIC';
  diagnostic.current_question.options[0].text = attack;
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: attack }, state: { diagnostic, error: attack, busy: '', assessmentId: 'a1', evolution: { items: [{ topic_name: attack, current: {} }] } } });
  assert.ok(html.includes('&lt;img')); assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('SECRET_KEY') && !html.includes('SECRET_RUBRIC'));
  assert.match(html, /role="alert"/); assert.match(html, /name="answer"/);
});

test('reports distinguish unavailable data, proxy metrics and observational retests', () => {
  const input = { mode: 'live', space: { id: 's1', name: 'Space' }, state: { busy: '', error: '',
    diagnostic: { ...trace(null), completion_reason: 'no_unseen_question_family' },
    evolution: { items: [] }, comparison: { scenarios: [] },
    replay: { policies: [], decisions: [], prediction: { predicted_observations: 0, eligible_observations: 0, mean_absolute_error: null }, observed_retests: { pair_count: 0, reason: 'no_eligible_retests' } } } };
  const html = ['diagnostic', 'evolution', 'comparison', 'replay'].map(section => renderLearning({ ...input, section })).join('');
  assert.match(html, /没有可用的新题族/); assert.match(html, /暂无/);
  assert.match(html, /代理指标/); assert.match(html, /因果/); assert.match(html, /独立/);
  assert.doesNotMatch(html, /null|undefined|NaN/);
});

test('comparison form validates distinct bounded budgets and horizon', () => {
  assert.deepEqual(comparisonPayload('20, 40', '7'), { budgets_minutes_per_day: [20, 40], horizon_days: 7 });
  for (const [budgets, days] of [['20,20', '7'], ['0,30', '7'], ['20,40', '31'], ['20,40', '1.5'], ['20,40,60,80,100,120', '7']])
    assert.throws(() => comparisonPayload(budgets, days));
});

test('ambiguous answer failure retries same immutable payload and key', async () => {
  const calls = []; let fail = true;
  const api = { createAssessment: async () => ({ assessment_id: 'a1', run_id: 'r1' }), waitRun: async () => {},
    assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }), diagnostic: async () => trace(),
    recordAttempt: async (id, body, options) => { calls.push({ body, key: options.key }); if (fail) throw new Error('结果未知'); return {}; } };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.start(); await controller.answer('A');
  assert.match(controller.snapshot().error, /结果未知/);
  await controller.answer('B');
  assert.equal(calls.length, 1); assert.match(controller.snapshot().error, /原答案/);
  fail = false; await controller.answer('A');
  assert.deepEqual(calls[0], calls[1]);
});

test('creating after an ambiguous network failure reuses the durable creation key', async () => {
  const storage = memory(), keys = [];
  const api = { createAssessment: async (id, body, options) => { keys.push(options.key); throw new Error('连接中断'); } };
  await createLearningController({ api, context, storage }).start();
  await createLearningController({ api, context, storage }).start();
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
});

test('report errors remain visible and stale errors cannot overwrite a new context', async () => {
  let selected = 's1', reject;
  const controller = createLearningController({ api: { evolution: () => new Promise((_, fail) => { reject = fail; }) },
    context: () => ({ mode: 'live', spaceId: selected, active: true }), storage: memory() });
  const first = controller.loadEvolution(); reject(new Error('请检查学习范围')); await first;
  assert.equal(controller.snapshot().error, '请检查学习范围');
  const second = controller.loadEvolution(); selected = 's2'; controller.sync(); reject(new Error('旧空间出错')); await second;
  assert.equal(controller.snapshot().error, ''); assert.equal(controller.snapshot().busy, '');
});

test('comparison renders chosen budgets after action rerenders the form', async () => {
  const controller = createLearningController({ api: { comparePlans: async () => ({ space_id: 's1', scenarios: [] }) }, context, storage: memory() });
  await controller.compare({ budgets_minutes_per_day: [30, 60], horizon_days: 14 });
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: controller.snapshot(), section: 'comparison' });
  assert.ok(html.includes('value="30,60"')); assert.ok(html.includes('value="14"'));
});

test('ambiguous creation after a completed round retains the new request key', async () => {
  let created = 0; const keys = [];
  const api = { createAssessment: async (id, body, options) => { created++; keys.push(options.key); if (created > 1) throw new Error('创建响应中断'); return { assessment_id: 'a1', run_id: 'r1' }; },
    waitRun: async () => {}, assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: 'completed' }),
    diagnostic: async () => ({ ...trace(null), status: 'completed' }), assessmentResult: async () => ({ question_results: [] }) };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.start(); await controller.start(); await controller.start();
  assert.notEqual(keys[0], keys[1]); assert.equal(keys[1], keys[2]);
});

test('stale mastery is not presented as a current score', () => {
  const html = renderLearning({ mode: 'live', section: 'evolution', space: { id: 's1', name: 'Space' }, state: { busy: '', error: '',
    evolution: { items: [{ topic_name: 'Changed revision', current: { mastery_score: 0.987, score_validity: 'stale', independent_evidence_count: 7, review_schedule: { selected_evidence_ids: [] } } }] } } });
  assert.ok(!html.includes('0.987'));
  assert.match(html, /历史均值/);
});

test('diagnostics progress in current page when session storage is unavailable', async () => {
  let answered = false;
  const storage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const api = { createAssessment: async () => ({ assessment_id: 'a1', run_id: 'r1' }), waitRun: async () => {},
    assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }), diagnostic: async () => trace(answered ? 'q2' : 'q1'),
    recordAttempt: async () => { answered = true; return {}; } };
  const controller = createLearningController({ api, context, storage });
  await controller.start(); await controller.answer('A');
  assert.equal(controller.snapshot().error, '');
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'q2');
});

test('completed task context enforces retest when submitted form selects an unsupported kind', async () => {
  let body;
  const controller = createLearningController({ storage: memory(), context: () => ({ mode: 'live', spaceId: 's1', active: true,
    assessmentContext: { space_id: 's1', plan_id: 'p1', task_id: 't1', topic_ids: ['topic'], kind: 'retest', allowed_kinds: ['retest'] } }),
    api: { space: async () => ({ id: 's1', topic_ids: ['topic'] }), topics: async () => [{ id: 'topic', source_refs: [{ chunk_id: 'chunk' }] }],
      createAssessment: async (_, request) => { body = request; throw new Error('stop after capture'); } } });
  await controller.loadKnowledge();
  await controller.start({ kind: 'diagnostic', count: '5', type: 'single_choice' });
  assert.equal(body.kind, 'retest');
  assert.equal(body.adaptive, false);
  assert.equal(body.allowed_kinds, undefined);
  assert.equal(controller.snapshot().setup.kind, 'retest');
  const html = renderLearning({ mode: 'live', space: { id: 's1' }, state: { ...controller.snapshot(), assessmentId: '', knowledge: { space: { bindings: [] }, topics: [{ id: 'topic' }], eligible: [{ id: 'topic' }] } } });
  assert.doesNotMatch(html.split('id="assessment-kind"')[1].split('</select>')[0], /value="diagnostic"|value="practice"/);
});
