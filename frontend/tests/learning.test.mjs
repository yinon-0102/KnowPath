import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning, comparisonPayload } from '../src/learning.js';
import { createApi } from '../src/api.js';

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
    waitRun: async id => calls.push(id), assessment: async () => ({ id: 'a1', space_id: 's1', status: step === 3 ? 'completed' : 'ready' }),
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
  assert.deepEqual(calls[2], { answers: [{ question_id: 'q1', expected_answer_revision: 0, answer: 'A' }] });
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'q2');
  await controller.answer('B');
  await controller.finalize();
  assert.equal(controller.snapshot().diagnostic.status, 'completed');
  assert.equal(controller.snapshot().result.question_results.length, 1);
});

test('resume restores same assessment and validates the owning space', async () => {
  const storage = memory(); let creates = 0;
  const api = { createAssessment: async () => { creates++; return { assessment_id: 'a1', run_id: 'r1' }; }, waitRun: async () => {},
    assessment: async () => ({ id: 'a1', space_id: 's1', status: 'ready' }), diagnostic: async () => trace() };
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

test('demo mode makes no requests and renders honest empty states', async () => {
  const controller = createLearningController({ api: new Proxy({}, { get: () => () => { throw new Error('network forbidden'); } }),
    context: () => ({ mode: 'demo', spaceId: 'demo', active: true }), storage: memory() });
  await controller.start(); await controller.loadEvolution();
  const html = renderLearning({ mode: 'demo', space: { id: 'demo', name: 'Demo' }, state: controller.snapshot() });
  assert.match(html, /真实学习数据/); assert.match(html, /不会生成示例诊断/);
  assert.doesNotMatch(html, /Which definition/);
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
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: { busy: '', error: '',
    diagnostic: { ...trace(null), completion_reason: 'no_unseen_question_family' },
    evolution: { items: [] }, comparison: { scenarios: [] },
    replay: { policies: [], decisions: [], prediction: { predicted_observations: 0, eligible_observations: 0, mean_absolute_error: null }, observed_retests: { pair_count: 0, reason: 'no_eligible_retests' } } } });
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
    assessment: async () => ({ id: 'a1', space_id: 's1', status: 'ready' }), diagnostic: async () => trace(),
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
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: controller.snapshot() });
  assert.ok(html.includes('value="30,60"')); assert.ok(html.includes('value="14"'));
});

test('ambiguous creation after a completed round retains the new request key', async () => {
  let created = 0; const keys = [];
  const api = { createAssessment: async (id, body, options) => { created++; keys.push(options.key); if (created > 1) throw new Error('创建响应中断'); return { assessment_id: 'a1', run_id: 'r1' }; },
    waitRun: async () => {}, assessment: async () => ({ id: 'a1', space_id: 's1', status: 'completed' }),
    diagnostic: async () => ({ ...trace(null), status: 'completed' }), assessmentResult: async () => ({ question_results: [] }) };
  const controller = createLearningController({ api, context, storage: memory() });
  await controller.start(); await controller.start(); await controller.start();
  assert.notEqual(keys[0], keys[1]); assert.equal(keys[1], keys[2]);
});

test('stale mastery is not presented as a current score', () => {
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: { busy: '', error: '',
    evolution: { items: [{ topic_name: 'Changed revision', current: { mastery_score: 0.987, score_validity: 'stale', independent_evidence_count: 7, review_schedule: { selected_evidence_ids: [] } } }] } } });
  assert.ok(!html.includes('0.987'));
  assert.match(html, /历史均值/);
});

test('diagnostics progress in current page when session storage is unavailable', async () => {
  let answered = false;
  const storage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const api = { createAssessment: async () => ({ assessment_id: 'a1', run_id: 'r1' }), waitRun: async () => {},
    assessment: async () => ({ id: 'a1', space_id: 's1', status: 'ready' }), diagnostic: async () => trace(answered ? 'q2' : 'q1'),
    recordAttempt: async () => { answered = true; return {}; } };
  const controller = createLearningController({ api, context, storage });
  await controller.start(); await controller.answer('A');
  assert.equal(controller.snapshot().error, '');
  assert.equal(controller.snapshot().diagnostic.current_question.id, 'q2');
});
