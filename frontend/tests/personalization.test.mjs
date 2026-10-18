import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning } from '../src/learning.js';
const memory = () => { const m = new Map(); return { getItem: k => m.get(k), setItem: (k,v) => m.set(k,v) }; };
const context = () => ({ spaceId: 's', active: true });

for (const policy of ['after_each_answer', 'after_round']) test(`leaving ${policy} ends the bound phase after the first answer`, async () => {
  const events = [], storage = memory(); let answered = false;
  storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s: { assessmentId: 'a' } }));
  const q = { id: 'q1', type: 'single_choice', options: [{ id: 'yes' }] };
  const c = createLearningController({ context, storage, api: {
    assessment: async () => ({ id: 'a', space_id: 's', status: 'in_progress', feedback_policy: policy, question_count: 2,
      questions: [q, { ...q, id: 'q2' }], answers: answered ? [{ question_id: 'q1' }] : [],
      question_feedback: answered && policy === 'after_each_answer' ? [{ question_id: 'q1', verdict: 'correct' }] : [] }),
    assessmentActivity: async (id, body) => { events.push({ id, ...body }); return {}; },
    recordAttempt: async () => { answered = true; return { question_feedback: policy === 'after_each_answer' ? [{ question_id: 'q1', verdict: 'correct' }] : [] }; }
  } });
  await c.resume(); await c.answer('yes'); c.dispose(); await Promise.resolve();
  const shown = events.filter(e => e.event === 'shown'), ended = events.filter(e => e.event === 'assessment_end');
  assert.ok(ended.length > 0, 'the phase has an explicit end after submitting its question');
  assert.ok(shown.every(e => e.phase_id && ended.some(end => end.id === e.id && end.phase_id === e.phase_id)));
});
test('resumed question reports shown and attempts never send browser elapsed', async () => {
  const events = [], attempts = [];
  const storage = memory(); storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s: { assessmentId: 'a', assessmentStatus: 'in_progress' } }));
  let answered = false;
  const c = createLearningController({ context, storage, api: { assessment: async () => ({ id: 'a', space_id: 's', adaptive: true, status: 'in_progress' }),
    diagnostic: async () => ({ assessment_id: 'a', status: 'in_progress', current_question: answered ? null : { id: 'q', type: 'single_choice', options: [{ id: 'yes' }] } }),
    assessmentActivity: async (id, body) => { events.push({ id, ...body }); return { paused: body.event === 'waiting', activity: { active_seconds: 7 } }; },
    recordAttempt: async (id, body) => { attempts.push(body); answered = true; } } });
  await c.resume(); assert.equal(events[0].event, 'shown'); assert.equal(events[0].question_id, 'q');
  await c.answer('yes'); assert.equal(events[1].event, 'waiting'); assert.ok(!('elapsed_seconds' in attempts[0].answers[0])); c.dispose();
});
test('completed assessment resumes the actual automatic adjustment run', async () => {
  const waited = [], storage = memory(); storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s: { assessmentId: 'a' } }));
  const c = createLearningController({ context, storage, api: { assessment: async () => ({ id: 'a', space_id: 's', adaptive: true, status: 'completed' }),
    diagnostic: async () => ({ assessment_id: 'a', status: 'completed', current_question: null }),
    assessmentResult: async () => ({ completion: { result: { adjustment: { status: 'queued', run_id: 'rule-run', id: 'adj' } } } }),
    waitRun: async id => { waited.push(id); return { status: 'succeeded', result: { adjustment: { status: 'applied', id: 'adj' } } }; } } });
  await c.resume(); assert.deepEqual(waited, ['rule-run']); assert.equal(c.snapshot().adjustment.status, 'applied'); c.dispose();
});
test('failed answer transport resumes the current question clock', async () => {
  const events = [], storage = memory(); storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s: { assessmentId: 'a' } }));
  const c = createLearningController({ context, storage, api: { assessment: async () => ({ id: 'a', space_id: 's', adaptive: true, status: 'in_progress' }),
    diagnostic: async () => ({ assessment_id: 'a', status: 'in_progress', current_question: { id: 'q', type: 'single_choice', options: [{ id: 'yes' }] } }),
    assessmentActivity: async (_, body) => { events.push(body.event); return {}; }, recordAttempt: async () => { throw new Error('暂时断开'); } } });
  await c.resume(); await c.answer('yes'); assert.deepEqual(events, ['shown', 'waiting', 'ready']); c.dispose();
});
test('schedule preview requires explicit apply and preserves snapshot guards', async () => {
  const calls = [];
  const c = createLearningController({ context, storage: memory(), api: { comparePlans: async () => ({ space_id: 's', comparison_id: 'c', snapshot: { plan_id: 'p', plan_version: 4, hash: 'hash' }, scenarios: [{ scenario_id: 'daily', can_apply: true, daily_minutes: 30, tasks: [{ id: 't', title: '完整路线节点' }] }] }),
    applySchedule: async (id, body, options) => { calls.push({ id, body, key: options.key }); return { plan_id: id }; } } });
  await c.compare({}); assert.equal(typeof c.previewSchedule, 'function'); await c.previewSchedule('daily'); assert.equal(calls.length, 0);
  await c.applySchedule(); assert.equal(calls.length, 1); assert.deepEqual(calls[0].body, { comparison_id: 'c', scenario_id: 'daily', expected_plan_version: 4, expected_snapshot_hash: 'hash' }); assert.ok(calls[0].key); c.dispose();
});
test('recommendation application is preceded by a server preview', async () => {
  const calls = [];
  const c = createLearningController({ context, storage: memory(), api: { recommendPolicy: async () => ({ id: 'r', space_id: 's', action: { kind: 'schedule' }, snapshot: { plan_version: 3 }, explanation: '证据支持检查预算' }),
    applyPolicyRecommendation: async (id, body) => { calls.push(body); return { status: body.preview ? 'preview' : 'applied' }; } } });
  assert.equal(typeof c.recommend, 'function'); await c.recommend(); await c.previewRecommendation(); assert.deepEqual(calls, [{ expected_plan_version: 3, preview: true }]); await c.applyRecommendation(); assert.equal(calls[1].preview, false); c.dispose();
});
test('new comparison renders full route and split allocation phases', () => {
  const html = renderLearning({ space: { id: 's', name: '空间' }, section: 'comparison', state: { comparison: { comparison_id: 'c', scenarios: [{ scenario_id: 'a', daily_minutes: 30, can_apply: true, coverage_count: 1, total_count: 2, tasks: [{ title: '路线节点', context: { duration_forecast: { study_minutes: 20, answer_minutes: 5 }, allocations: [{ date: '2026-10-12', phase: 'study', minutes: 20 }, { date: '2026-10-13', phase: 'answer', minutes: 5 }] } }] }] } } });
  assert.match(html, /路线节点/); assert.match(html, /2026-10-13/); assert.match(html, /预览此排程/);
});

test('budget route shows the persisted context title instead of an opaque task ID', () => {
  const html = renderLearning({ space: { id: 's' }, section: 'comparison', state: { comparison: { scenarios: [{ scenario_id: 'a', daily_minutes: 15, tasks: [{ id: 'opaque-task-id', context: { title: '变量与赋值', duration_forecast: {} } }] }] } } });
  assert.match(html, /变量与赋值/);
  assert.doesNotMatch(html, /<strong>opaque-task-id<\/strong>/);
});
