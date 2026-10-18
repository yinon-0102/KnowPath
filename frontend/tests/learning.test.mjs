import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning, comparisonPayload, assessmentTopics, assessmentPayload, assessmentTaskCompletion, renderCompletionRecovery } from '../src/learning.js';
import { createApi, ApiError } from '../src/api.js';

test('completion recovery persists a usable action with distinct saving, unknown and saved messages', () => {
  const target = { spaceId: 's', planId: 'p', taskId: 't', source: 'manual' };
  for (const [status, phrase, label] of [['saving', '正在保存', '查询保存结果'], ['querying', '正在查询', '查询保存结果'], ['unknown', '结果待确认', '查询保存结果'], ['navigation_pending', '已完成', '继续学习']]) {
    const html = renderCompletionRecovery([{ target, entry: { status, busy: status === 'saving' || status === 'querying', pending: status === 'unknown' ? {} : null, navigationPending: status === 'navigation_pending' } }]);
    assert.match(html, new RegExp(phrase)); assert.match(html, new RegExp(label)); assert.match(html, /data-action="completion-recover"/);
    if (status === 'saving' || status === 'querying') assert.match(html, /disabled/);
    if (status === 'unknown') assert.match(html, /data-action="completion-retry"/);
  }
  assert.equal(renderCompletionRecovery([]), '');
});

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

const recoveryRound = (id, extra = {}) => ({ id, space_id: 's1', kind: 'retest', status: 'in_progress', question_count: 5,
  feedback_policy: 'after_round', questions: Array.from({ length: 5 }, (_, i) => ({ id: `${id}-q${i + 1}`, type: 'single_choice', options: [{ id: 'A', text: 'A' }] })),
  answers: Array.from({ length: 4 }, (_, i) => ({ question_id: `${id}-q${i + 1}` })), ...extra });

test('empty storage recovers the server round at four of five and explicit selection checks owner', async () => {
  let creates = 0;
  const round = recoveryRound('a', { plan_id: 'p', task_id: 't', learning_session_id: 'ss' });
  const api = { recoverableAssessments: async () => ({ items: [{ id: 'a', space_id: 's1' }] }), assessment: async () => round,
    createAssessment: async () => { creates++; } };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.recover({ assessmentId: 'a', planId: 'p', taskId: 't', learningSessionId: 'ss' });
  assert.equal(controller.snapshot().assessmentId, 'a');
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'a-q5');
  assert.deepEqual(controller.snapshot().diagnostic.progress, { answered: 4, target: 5 });
  assert.equal(controller.snapshot().answerDraftKey.includes('p'), true);
  assert.equal(creates, 0);
  await controller.recover({ assessmentId: 'a', taskId: 'other', planId: 'p' });
  assert.match(controller.snapshot().error, /归属|任务/);
  assert.equal(controller.snapshot().diagnostic, null);
});

test('multiple server rounds require selection and same-space late answers preserve both round intents', async () => {
  const storage = memory(); let release, sending;
  const waiting = new Promise(resolve => { sending = resolve; });
  const api = { recoverableAssessments: async () => ({ items: ['a', 'b'].map(id => ({ id, space_id: 's1', status: 'in_progress', progress: { answered: 4, target: 5 } })) }),
    assessment: async id => recoveryRound(id),
    recordAttempt: async () => { sending(); return new Promise(resolve => { release = resolve; }); } };
  const controller = createLearningController({ api, context, storage });
  await controller.recover();
  assert.equal(controller.snapshot().assessmentId, '');
  assert.equal(controller.snapshot().recoverableAssessments.length, 2);
  assert.match(renderLearning({ space: { id: 's1' }, state: controller.snapshot() }), /data-action="learning-select"/);
  await controller.resume('a'); controller.draftAnswer('A');
  const pending = controller.answer('A'); await waiting;
  await controller.resume('b'); controller.draftAnswer('A');
  release({}); await pending;
  assert.equal(controller.snapshot().assessmentId, 'b');
  assert.equal(controller.snapshot().pendingAnswer, null);
  assert.equal(controller.snapshot().answerDraft.assessmentId, 'b');
  await controller.resume('a');
  assert.equal(controller.snapshot().pendingAnswer.acknowledged, true);
  await controller.resume('b');
  assert.equal(controller.snapshot().answerDraft.assessmentId, 'b');
});

test('only confirmed pre-create rejection unlocks a new body and key', async () => {
  for (const error of [new ApiError('invalid', 422, 'INVALID_REQUEST'), new ApiError('scope', 409, 'TOPIC_OUT_OF_SCOPE')]) {
    const calls = [];
    const controller = createLearningController({ context, storage: memory(), api: {
      space: async () => ({ id: 's1' }), topics: async () => [{ id: 't', source_refs: [{}] }],
      createAssessment: async (_, body, options) => { calls.push({ body, key: options.key }); throw error; },
    } });
    await controller.loadKnowledge(); await controller.start({ kind: 'practice', count: '5', type: 'single_choice' });
    await controller.start({ kind: 'retest', count: '7', type: 'short_answer' });
    assert.notEqual(calls[0].key, calls[1].key);
    assert.equal(calls[1].body.question_count, 7); assert.equal(calls[1].body.kind, 'retest');
  }
});

test('unknown create outcomes keep original immutable request across settings, route and reload', async () => {
  for (const error of [new ApiError('network', 0, 'NETWORK'), new ApiError('server', 503, 'HTTP_ERROR'), new ApiError('conflict', 409, 'IDEMPOTENCY_CONFLICT'), null]) {
    const calls = [], storage = memory(); let routeKey = 'one';
    const api = { createAssessment: async (_, body, options) => { calls.push({ body: structuredClone(body), key: options.key }); if (error) throw error; return {}; } };
    const controller = createLearningController({ api, storage, context: () => ({ ...context(), routeKey }) });
    await controller.start(); routeKey = 'two'; controller.sync();
    await controller.start(); await createLearningController({ api, storage, context }).start();
    assert.deepEqual(calls[0], calls[1]); assert.deepEqual(calls[1], calls[2]);
    assert.match(renderLearning({ space: { id: 's1' }, state: controller.snapshot() }), /创建结果尚未确认/);
  }
});

test('unknown creation retries skip fresh owner validation and keep the original body and key', async () => {
  let ownerValidations = 0, routeKey = 'task-a'; const calls = [];
  const controller = createLearningController({ context: () => ({ ...context(), routeKey, assessmentContext: null }), storage: memory(),
    prepareCreation: async () => {
      ownerValidations++;
      if (ownerValidations > 1) throw new Error('current plan no longer allows creation');
      return { space_id: 's1', plan_id: 'p', task_id: 'a', learning_session_id: 'session', topic_ids: ['topic'] };
    }, api: { space: async () => ({ id: 's1', topic_ids: ['topic'] }), topics: async () => [{ id: 'topic', source_refs: [{}] }],
      createAssessment: async (_, body, options) => { calls.push({ body: structuredClone(body), key: options.key }); throw new ApiError('unknown', 0, 'NETWORK'); } } });
  await controller.loadKnowledge(); await controller.start({ kind: 'practice', count: '5', type: 'single_choice' });
  routeKey = 'task-b'; controller.sync();
  await controller.start({ kind: 'retest', count: '7', type: 'short_answer' });
  assert.equal(ownerValidations, 1); assert.equal(calls.length, 2); assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[1].body.task_id, 'a'); assert.equal(calls[1].body.question_count, 5);
});

test('unknown accepted creation is reconciled by exact ID before starting a new intent after recovered completion', async () => {
  const storage = memory(), calls = []; let fail = true;
  const api = { space: async () => ({ id: 's1' }), topics: async () => [{ id: 'topic', source_refs: [{}] }],
    createAssessment: async (_, body, options) => {
      calls.push({ body: structuredClone(body), key: options.key });
      if (fail) { fail = false; throw new ApiError('accepted but lost', 503, 'HTTP_ERROR'); }
      return { assessment_id: calls.length < 3 ? 'a' : 'b', run_id: 'run' };
    }, waitRun: async () => {},
    recoverableAssessments: async () => ({ items: [{ id: 'a', space_id: 's1' }] }),
    assessment: async id => recoveryRound(id, { status: id === 'a' ? 'completed' : 'in_progress' }),
    assessmentResult: async id => ({ assessment_id: id, question_results: [] }),
  };
  const first = createLearningController({ api, context, storage }); await first.start();
  const restored = createLearningController({ api, context, storage }); await restored.loadKnowledge(); await restored.recover();
  assert.equal(restored.snapshot().assessmentId, 'a');
  await restored.start({ kind: 'practice', count: '7', type: 'single_choice' });
  assert.equal(calls.length, 3); assert.deepEqual(calls[0], calls[1]);
  assert.notEqual(calls[1].key, calls[2].key); assert.equal(calls[2].body.question_count, 7);
  assert.equal(restored.snapshot().assessmentId, 'b');
});

test('reconciling an unknown creation that is now explicitly rejected releases its original intent', async () => {
  let reject = true; const calls = [];
  const controller = createLearningController({ context, storage: memory(), api: {
    space: async () => ({ id: 's1' }), topics: async () => [{ id: 'topic', source_refs: [{}] }],
    createAssessment: async (_, body, options) => { calls.push({ body, key: options.key }); throw reject
      ? new ApiError('unknown', 0, 'NETWORK') : new ApiError('outside', 422, 'TOPIC_OUT_OF_SCOPE'); },
    assessment: async id => recoveryRound(id, { status: 'completed' }), assessmentResult: async () => ({ question_results: [] }),
  } });
  await controller.loadKnowledge(); await controller.start({ count: '5', type: 'single_choice', kind: 'practice' });
  await controller.resume('c'); reject = false;
  await controller.start({ count: '7', type: 'single_choice', kind: 'practice' });
  await controller.start({ count: '8', type: 'single_choice', kind: 'practice' });
  assert.equal(calls[0].key, calls[1].key); assert.notEqual(calls[1].key, calls[2].key);
  assert.equal(calls[2].body.question_count, 8);
});

test('setup initialization gate and revision keep late defaults from replacing a user setting', async () => {
  let setupReady = false, creates = 0;
  const controller = createLearningController({ storage: memory(), context: () => ({ ...context(), setupReady }),
    api: { createAssessment: async () => { creates++; return {}; } } });
  await controller.start(); assert.equal(creates, 0); assert.match(controller.snapshot().error, /初始化/);
  const revision = controller.snapshot().setupRevision;
  controller.configure({ kind: 'retest', count: '7' });
  setupReady = true; controller.configureDefaults({ kind: 'practice' }, revision);
  assert.equal(controller.snapshot().setup.kind, 'retest'); assert.equal(controller.snapshot().setup.count, '7');
});

test('failed initialization leaves a usable recovery retry while creation stays disabled', () => {
  const controller = createLearningController({ context: () => ({ ...context(), setupReady: false }), api: {}, storage: memory() });
  controller.initializeError(new Error('temporary offline'));
  const html = renderLearning({ space: { id: 's1' }, state: controller.snapshot() });
  assert.match(html, /data-action="learning-initialize"(?![^>]*disabled)/);
  assert.match(html, /id="learning-start-form"[^]*?<fieldset disabled/);
});

test('a late accepted create cannot replace another same-space round or its pending answer', async () => {
  let release; const storage = memory();
  const api = { createAssessment: async () => new Promise(resolve => { release = resolve; }), assessment: async id => recoveryRound(id),
    recordAttempt: async () => { throw new ApiError('unknown', 0, 'NETWORK'); } };
  const controller = createLearningController({ api, context, storage });
  const creating = controller.start();
  await controller.resume('b'); await controller.answer('A');
  release({ assessment_id: 'a', run_id: 'r' }); await creating;
  assert.equal(controller.snapshot().assessmentId, 'b'); assert.equal(controller.snapshot().pendingAnswer.answer, 'A');
  await controller.resume('a'); assert.equal(controller.snapshot().diagnostic.current_question.id, 'a-q5');
  await controller.resume('b'); assert.equal(controller.snapshot().pendingAnswer.answer, 'A');
});

test('late create receipt preserves an already recovered same-round answer intent', async () => {
  let release;
  const controller = createLearningController({ context, storage: memory(), api: {
    createAssessment: async () => new Promise(resolve => { release = resolve; }), assessment: async id => recoveryRound(id),
    recordAttempt: async () => { throw new ApiError('unknown', 0, 'NETWORK'); },
  } });
  const creating = controller.start(); await controller.resume('a'); await controller.answer('A');
  release({ assessment_id: 'a', run_id: 'r' }); await creating;
  await controller.resume(); assert.equal(controller.snapshot().pendingAnswer.answer, 'A');
});

test('late shown activity response cannot restore the departed round pending answer into the new round', async () => {
  let release, slow = false;
  const controller = createLearningController({ context, storage: memory(), api: {
    assessment: async id => recoveryRound(id), recordAttempt: async () => { throw new ApiError('unknown', 0, 'NETWORK'); },
    assessmentActivity: async (id, body) => id === 'a' && slow && body.event === 'shown'
      ? new Promise(resolve => { release = resolve; }) : {},
  } });
  await controller.resume('a'); await controller.answer('A'); await controller.resume('b');
  slow = true; const refreshing = controller.resume('a');
  await new Promise(resolve => setImmediate(resolve));
  await controller.resume('b'); release({}); await refreshing;
  assert.equal(controller.snapshot().assessmentId, 'b'); assert.equal(controller.snapshot().pendingAnswer, null);
  controller.dispose();
});

test('an old answer receipt cannot replace a newer pending question after leaving and returning to the same round', async () => {
  let release, reads = 4, sends = 0;
  const api = { assessment: async id => recoveryRound(id, { question_count: 6,
      questions: Array.from({ length: 6 }, (_, i) => ({ id: `${id}-q${i + 1}`, type: 'single_choice', options: [{ id: 'A', text: 'A' }] })),
      answers: Array.from({ length: reads }, (_, i) => ({ question_id: `${id}-q${i + 1}` })) }),
    recordAttempt: async () => { if (++sends === 1) return new Promise(resolve => { release = resolve; }); throw new ApiError('unknown', 0, 'NETWORK'); } };
  const controller = createLearningController({ context, storage: memory(), api });
  await controller.resume('a'); const original = controller.answer('A');
  await controller.resume('b'); reads = 5; await controller.resume('a'); await controller.answer('A');
  const current = structuredClone(controller.snapshot().pendingAnswer);
  release({}); await original; await controller.resume();
  assert.deepEqual(controller.snapshot().pendingAnswer, current);
  assert.equal(current.questionId, 'a-q6');
});

test('late GET and finalize from one round cannot replace another same-space round', async () => {
  let releaseGet, releaseFinalize, slow = false;
  const controller = createLearningController({ context, storage: memory(), api: {
    assessment: async id => slow && id === 'a' ? new Promise(resolve => { releaseGet = resolve; }) : recoveryRound(id, { answers: Array.from({ length: 5 }, (_, i) => ({ question_id: `${id}-q${i + 1}` })) }),
    finalizeAssessment: async () => new Promise(resolve => { releaseFinalize = resolve; }),
  } });
  await controller.resume('a');
  const finalizing = controller.finalize(); await controller.resume('b');
  releaseFinalize({ run_id: 'done' }); await finalizing;
  assert.equal(controller.snapshot().assessmentId, 'b'); assert.equal(controller.snapshot().pendingFinalize, null);
  slow = true; const oldGet = controller.resume('a'); await controller.resume('b');
  releaseGet(recoveryRound('a')); await oldGet;
  assert.equal(controller.snapshot().assessmentId, 'b'); assert.equal(controller.snapshot().diagnostic.current_question, null);
});

test('new tab recovers generating run and completed lost-finalize response using the same ID', async () => {
  const reads = [], polls = []; let status = 'generating';
  const api = { assessment: async id => { reads.push(id); return recoveryRound(id, { status, run_id: 'original-run' }); },
    waitRun: async id => { polls.push(id); status = 'ready'; },
    assessmentResult: async id => ({ assessment_id: id, question_results: [{ evidence_id: 'original-evidence' }] }),
    finalizeAssessment: async () => { throw new Error('recovery GET must not finalize'); } };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.recover({ assessmentId: 'a' }); assert.deepEqual(polls, ['original-run']);
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'a-q5');
  status = 'completed';
  await createLearningController({ api, context, storage: memory() }).recover({ assessmentId: 'a' });
  assert.ok(reads.every(id => id === 'a'));
});

test('new tab recovers a pending generation and cancels the original sealed run', async () => {
  const cancellations = []; let cancelled = false, creates = 0;
  const controller = createLearningController({ context: () => ({ ...context(), setupReady: false }), storage: memory(), api: {
    assessment: async id => ({ id, space_id: 's1', status: cancelled ? 'cancelled' : 'generating', run_id: 'original-run' }),
    waitRun: async id => { throw new ApiError(cancelled ? 'cancelled' : 'still generating', 200,
      cancelled ? 'RUN_CANCELLED' : 'RUN_PENDING', { runId: id, runStatus: cancelled ? 'cancelled' : 'running' }); },
    cancelRun: async id => { cancellations.push(id); cancelled = true; },
    createAssessment: async () => { creates++; },
  } });
  await controller.recover({ assessmentId: 'a' });
  assert.equal(controller.snapshot().assessmentStatus, 'generating'); assert.match(controller.snapshot().error, /仍在生成/);
  assert.equal(controller.snapshot().setupReady, false);
  const html = renderLearning({ space: { id: 's1' }, state: controller.snapshot() });
  assert.match(html, /<button[^>]*data-action="learning-cancel"[^>]*>/);
  assert.doesNotMatch(html.match(/<button[^>]*data-action="learning-cancel"[^>]*>/)[0], /disabled/);
  await controller.cancelGeneration();
  assert.deepEqual(cancellations, ['original-run']); assert.equal(controller.snapshot().assessmentId, 'a');
  assert.equal(controller.snapshot().assessmentStatus, 'cancelled'); assert.equal(creates, 0);
});

test('recovering a round rejects a generation run that conflicts with its saved receipt', async () => {
  const storage = memory();
  storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s1: { assessmentId: 'a', runId: 'original-run' } }));
  let polls = 0;
  const controller = createLearningController({ context, storage, api: {
    assessment: async id => ({ id, space_id: 's1', status: 'generating', run_id: 'foreign-run' }),
    waitRun: async () => { polls++; },
  } });
  await controller.recover({ assessmentId: 'a' });
  assert.match(controller.snapshot().error, /出题任务.*不一致/); assert.equal(polls, 0);
});

test('legacy detail without association keeps the owner captured before create awaits', async () => {
  let owner = { space_id: 's1', plan_id: 'p1', task_id: 't1', topic_ids: ['topic'] }, release;
  const controller = createLearningController({ context: () => ({ ...context(), assessmentContext: owner }), storage: memory(), api: {
    space: async () => ({ id: 's1' }), topics: async () => [{ id: 'topic', source_refs: [{}] }],
    createAssessment: async () => new Promise(resolve => { release = resolve; }), waitRun: async () => {}, assessment: async () => recoveryRound('a'),
  } });
  await controller.loadKnowledge(); const pending = controller.start();
  owner = { ...owner, task_id: 't2' }; release({ assessment_id: 'a', run_id: 'r' }); await pending;
  controller.draftAnswer('A'); assert.equal(controller.snapshot().answerDraft.taskId, 't1');
});

test('completed linked assessment offers explicit task confirmation and reinforcement', () => {
  const plan = { id: 'p1', space_id: 's1', status: 'ready', version: 1, tasks: [{ id: 'task1', status: 'pending' }, { id: 'task2', status: 'pending' }] };
  const state = { busy: '', error: '', assessmentId: 'a1', assessment: { id: 'a1', space_id: 's1', plan_id: 'p1', task_id: 'task1', status: 'completed' },
    diagnostic: { assessment_id: 'a1', status: 'completed', progress: { answered: 5, target: 5 } }, result: { question_results: Array(5).fill({ verdict: 'correct' }) } };
  const html = renderLearning({ space: { id: 's1' }, state, plan });
  assert.match(html, /确认本节点完成并继续/); assert.match(html, /继续巩固/);
  assert.match(html, /仍需.*确认|确认.*任务完成/);
  const last = renderLearning({ space: { id: 's1' }, state: { ...state, assessment: { ...state.assessment, task_id: 'task2' } }, plan: { ...plan, tasks: [{ ...plan.tasks[0], status: 'completed' }, plan.tasks[1]] } });
  assert.match(last, /查看本轮总结/);
  assert.doesNotMatch(renderLearning({ space: { id: 's1' }, state: { ...state, assessment: { ...state.assessment, task_id: 'foreign' } }, plan }), /确认本节点完成并继续/);
});

test('result completion retains the server association after route context is switched', async () => {
  const storage = memory(); storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s1: { assessmentId: 'a1' } }));
  let association = { space_id: 's1', plan_id: 'p1', task_id: 'other' };
  const controller = createLearningController({ storage, context: () => ({ ...context(), assessmentContext: association }), api: {
    assessment: async () => ({ id: 'a1', space_id: 's1', plan_id: 'p1', task_id: 'original', status: 'completed' }),
    assessmentResult: async () => ({ assessment_id: 'a1', question_results: [] }),
  } });
  await controller.resume(); association = null;
  const plan = { id: 'p1', space_id: 's1', status: 'ready', version: 1, tasks: [{ id: 'original', status: 'pending' }, { id: 'other', status: 'pending' }] };
  assert.equal(assessmentTaskCompletion({ id: 's1' }, controller.snapshot(), plan).taskId, 'original');
  assert.equal(assessmentTaskCompletion({ id: 's1' }, controller.snapshot(), { ...plan, tasks: [{ id: 'original', status: 'completed' }] }), null);
});

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
    assert.equal(html.split('<section class="panel learning-panel"').length - 1, section === 'replay' ? 2 : 1);
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
  assert.deepEqual(calls[2], { include_feedback: true, answers: [{ question_id: 'q1', expected_answer_revision: 0, answer: 'A' }] });
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
