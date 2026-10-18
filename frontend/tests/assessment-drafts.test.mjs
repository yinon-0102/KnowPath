import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning } from '../src/learning.js';

const memory = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }; };
const context = () => ({ spaceId: 's1', active: true });
function fixture(type = 'single_choice') {
  let question = { id: 'q1', answer_revision: 0, type, prompt: 'Explain the definition', options: [{ id: 'A', text: 'First' }, { id: 'B', text: 'Second' }] };
  let status = 'in_progress';
  const events = [];
  const api = {
    createAssessment: async () => ({ assessment_id: 'a1', run_id: 'r1' }), waitRun: async () => {},
    space: async () => ({ id: 's1' }), topics: async () => [{ id: 't1', source_refs: [{}] }],
    assessment: async () => ({ id: 'a1', space_id: 's1', adaptive: true, status }),
    diagnostic: async () => ({ assessment_id: 'a1', status, current_question: question, progress: { answered: 0, target: 5 } }),
    assessmentActivity: async (_id, body) => { events.push(body); return { paused: ['hidden', 'disconnect', 'pause'].includes(body.event), activity: { active_seconds: events.length } }; },
    assessmentResult: async () => ({ question_results: [] }),
  };
  return { api, events, question: value => { question = value; }, complete: () => { status = 'completed'; question = null; } };
}

for (const [type, answer] of [['single_choice', 'B'], ['short_answer', 'Draft <definition>\nwith details']]) {
  test(`${type} draft survives the 30 second heartbeat, visibility, connectivity and reload`, async t => {
    let tick, interval, renders = 0, activityUpdates = 0;
    t.mock.method(globalThis, 'setInterval', (callback, ms) => { tick = callback; interval = ms; return { unref() {} }; });
    t.mock.method(globalThis, 'clearInterval', () => {});
    const { api, events } = fixture(type), storage = memory();
    const controller = createLearningController({ api, context, storage, changed: () => { renders++; }, activityChanged: () => { activityUpdates++; } });
    await controller.start();
    controller.draftAnswer?.(answer);
    const priorRenders = renders;
    assert.equal(interval, 30000);
    await tick();
    for (const event of ['hidden', 'visible', 'disconnect', 'reconnect']) await controller.activityEvent(event);
    assert.equal(renders, priorRenders, 'activity updates must retain the mounted answer form');
    assert.equal(controller.snapshot().answerDraft?.answer, answer);
    assert.equal(controller.snapshot().pendingAnswer, null, 'editing is not a submission');
    assert.ok(activityUpdates > 0);
    assert.deepEqual(events.slice(-5).map(event => event.event), ['heartbeat', 'hidden', 'visible', 'disconnect', 'reconnect']);
    await controller.resume();
    assert.equal(controller.snapshot().answerDraft?.answer, answer);
    const restored = createLearningController({ api, context, storage });
    await restored.resume();
    const html = renderLearning({ space: { id: 's1' }, state: restored.snapshot() });
    if (type === 'single_choice') assert.match(html, /value="B"[^>]*checked/);
    else assert.match(html, />Draft &lt;definition&gt;\nwith details<\/textarea>/);
    controller.dispose(); restored.dispose();
  });
}

test('drafts belong to the exact space, task, assessment, question and answer revision', async () => {
  let spaceId = 's1', taskId = 'task1';
  const storage = memory(), f = fixture('short_answer');
  const scopedContext = () => ({ spaceId, active: true, assessmentContext: { space_id: spaceId, plan_id: 'p1', task_id: taskId } });
  f.api.assessment = async () => ({ id: 'a1', space_id: 's1', plan_id: 'p1', task_id: 'task1', adaptive: true, status: 'in_progress' });
  const controller = createLearningController({ api: f.api, context: scopedContext, storage });
  await controller.loadKnowledge(); await controller.start(); controller.draftAnswer?.('original draft');
  taskId = 'task2'; assert.equal(controller.snapshot().answerDraft?.answer, 'original draft', 'setup changes cannot move the sealed round owner');
  taskId = 'task1'; assert.equal(controller.snapshot().answerDraft?.answer, 'original draft');
  spaceId = 's2'; assert.equal(controller.snapshot().answerDraft, null);
  spaceId = 's1'; await controller.resume(); assert.equal(controller.snapshot().answerDraft?.answer, 'original draft');
  f.question({ id: 'q1', type: 'short_answer', answer_revision: 1, prompt: 'Changed revision' });
  await controller.resume(); assert.equal(controller.snapshot().answerDraft, null);
  controller.draftAnswer?.('revised draft');
  f.question({ id: 'q2', type: 'short_answer', answer_revision: 0, prompt: 'Next question' });
  await controller.resume(); assert.equal(controller.snapshot().answerDraft, null);
  f.question({ id: 'q1', type: 'short_answer', answer_revision: 1, prompt: 'Previous question' });
  await controller.resume(); assert.equal(controller.snapshot().answerDraft, null, 'advancing clears the departed question draft');
  controller.draftAnswer?.('finished draft'); f.complete(); await controller.resume();
  assert.equal(controller.snapshot().answerDraft, null);
  controller.dispose();
});

test('draft cleanup leaves another space and task draft intact', async () => {
  const storage = memory(), a = fixture('short_answer'), b = fixture('short_answer');
  const first = createLearningController({ api: a.api, context, storage });
  const secondContext = () => ({ spaceId: 's2', active: true });
  b.api.assessment = async () => ({ id: 'a1', space_id: 's2', adaptive: true, status: 'in_progress' });
  const second = createLearningController({ api: b.api, context: secondContext, storage });
  await first.start(); await second.start();
  first.draftAnswer?.('space one'); second.draftAnswer?.('space two');
  a.complete(); await first.resume();
  const restored = createLearningController({ api: b.api, context: secondContext, storage });
  await restored.resume(); assert.equal(restored.snapshot().answerDraft?.answer, 'space two');
  first.dispose(); second.dispose(); restored.dispose();
});

test('an accepted request with a lost response keeps its immutable body and key while draft edits stay separate', async () => {
  const storage = memory(), f = fixture('short_answer'), calls = [], evidence = new Map();
  f.question({ id: 'q1', type: 'short_answer', answer_revision: 3, prompt: 'Explain' });
  f.api.recordAttempt = async (id, body, options) => {
    calls.push({ id, body: structuredClone(body), key: options.key });
    if (!evidence.has(options.key)) evidence.set(options.key, structuredClone(body));
    throw new Error('accepted response lost');
  };
  const first = createLearningController({ api: f.api, context, storage });
  await first.start(); first.draftAnswer?.('original answer'); await first.answer('original answer');
  const pending = structuredClone(first.snapshot().pendingAnswer);
  first.draftAnswer?.('edited after timeout');
  assert.deepEqual(first.snapshot().pendingAnswer, pending);
  assert.equal(first.snapshot().answerDraft?.answer, 'edited after timeout');
  await first.answer('edited after timeout'); assert.equal(calls.length, 1, 'a new answer cannot replace an unresolved submission');
  const restored = createLearningController({ api: f.api, context, storage });
  await restored.resume(); await restored.answer('original answer');
  assert.equal(calls.length, 2); assert.deepEqual(calls[0], calls[1]); assert.equal(evidence.size, 1);
  assert.deepEqual(calls[0].body, { include_feedback: true, answers: [{ question_id: 'q1', expected_answer_revision: 3, answer: 'original answer' }] });
  first.dispose(); restored.dispose();
});

test('a readable old storage value cannot overwrite a draft when writing exceeds quota', async () => {
  const storage = memory(), f = fixture('short_answer');
  storage.setItem('knowpath-assessment-drafts-v1', '{}');
  storage.setItem = () => { throw new Error('QuotaExceededError'); };
  const controller = createLearningController({ api: f.api, context, storage });
  await controller.start(); controller.draftAnswer('kept in this page');
  assert.equal(controller.snapshot().answerDraft?.answer, 'kept in this page');
  await controller.resume(); assert.equal(controller.snapshot().answerDraft?.answer, 'kept in this page');
  controller.dispose();
});
