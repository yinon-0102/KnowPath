import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError } from '../src/api.js';
import { createLearningController, renderLearning } from '../src/learning.js';

const context = () => ({ mode: 'live', spaceId: 's1', active: true });
const memory = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }; };
const unsupported = () => new ApiError('请求参数校验失败', 400, 'INVALID_REQUEST', {
  errors: [{ type: 'extra_forbidden', loc: ['body', 'adaptive'], msg: 'Extra inputs are not permitted' }],
});
const questions = Array.from({ length: 5 }, (_, i) => ({ id: 'q' + i, type: 'single_choice',
  prompt: 'Question ' + i, options: [{ id: 'A', text: 'Option A' }, { id: 'B', text: 'Option B' }] }));

test('legacy backend accepts standard creation and completes five sequential questions without diagnostic endpoint', async t => {
  const calls = [], answers = [], keys = []; let complete = false;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const path = new URL(url).pathname, body = options.body ? JSON.parse(options.body) : null;
    calls.push({ path, body });
    const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
    if (path.endsWith('/learning-spaces/s1/assessments')) {
      keys.push(options.headers['Idempotency-Key']);
      if (body.adaptive) return json({ error: unsupported() }, 400);
      assert.deepEqual(body, { kind: 'diagnostic', question_count: 5, question_types: ['single_choice'] });
      return json({ assessment_id: 'a1', run_id: 'r1' }, 202);
    }
    if (path.includes('/runs/')) return json({ status: 'succeeded' });
    if (path.endsWith('/assessments/a1')) return json({ id: 'a1', space_id: 's1', status: complete ? 'completed' : answers.length ? 'in_progress' : 'ready', questions, answers, question_count: 5 });
    if (path.endsWith('/attempts')) { answers.push(...body.answers); return json({ status: 'recorded' }); }
    if (path.endsWith('/finalize')) { complete = true; return json({ run_id: 'final' }, 202); }
    if (path.endsWith('/result')) return json({ question_results: answers.map(a => ({ question_id: a.question_id, verdict: 'correct' })) });
    assert.fail('Unexpected endpoint: ' + path);
  });
  const controller = createLearningController({ api: createApi(() => ''), context, storage: memory() });
  await controller.start();
  assert.equal(controller.snapshot().error, '');
  assert.notEqual(keys[0], keys[1]);
  assert.equal(controller.snapshot().diagnostic.mode, 'standard');
  for (let i = 0; i < questions.length; i++) {
    assert.equal(controller.snapshot().diagnostic.current_question.id, questions[i].id);
    const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: controller.snapshot() });
    assert.match(html, /学习测评/);
    assert.match(html, /按题目顺序作答/);
    if (i < 4) assert.ok(!html.includes('Question ' + (i + 1)));
    await controller.answer('A');
  }
  assert.equal(controller.snapshot().diagnostic.current_question, null);
  assert.equal(controller.snapshot().diagnostic.progress.answered, 5);
  await controller.finalize();
  assert.equal(controller.snapshot().diagnostic.status, 'completed');
  assert.equal(controller.snapshot().result.question_results.length, 5);
  assert.ok(!calls.some(call => call.path.endsWith('/diagnostic')));
});

test('legacy fallback with unknown response preserves the standard body and key after reload', async () => {
  const storage = memory(), calls = [];
  const api = { createAssessment: async (_, body, options) => {
    calls.push({ body, key: options.key });
    if (body.adaptive) throw unsupported();
    throw new ApiError('请求超时', 0, 'TIMEOUT');
  } };
  await createLearningController({ api, context, storage }).start();
  await createLearningController({ api, context, storage }).start();
  assert.equal(calls.length, 3);
  assert.notEqual(calls[0].key, calls[1].key);
  assert.deepEqual(calls[1], calls[2]);
  assert.equal(calls[1].body.adaptive, undefined);
});

test('unrelated schema errors and network failures do not silently switch assessment modes', async () => {
  for (const error of [new ApiError('连接失败', 0, 'NETWORK'),
    new ApiError('invalid', 400, 'INVALID_REQUEST', { errors: [{ type: 'value_error', loc: ['body', 'question_count'] }] }),
    new ApiError('invalid', 400, 'INVALID_REQUEST', { errors: [...unsupported().details.errors, { type: 'missing', loc: ['body', 'kind'] }] })]) {
    let creates = 0;
    const controller = createLearningController({ context, storage: memory(), api: { createAssessment: async () => { creates++; throw error; } } });
    await controller.start();
    assert.equal(creates, 1);
    assert.equal(controller.snapshot().assessmentId, '');
  }
});

test('a standard assessment with no published questions never exposes a completion action', async () => {
  const storage = memory();
  storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({ s1: { assessmentId: 'a1' } }));
  const controller = createLearningController({ context, storage, api: {
    assessment: async () => ({ id: 'a1', space_id: 's1', status: 'ready', question_count: 5, questions: [], answers: [] }),
  } });
  await controller.resume();
  assert.match(controller.snapshot().error, /尚未返回可作答题目/);
  const html = renderLearning({ mode: 'live', space: { id: 's1', name: 'Space' }, state: controller.snapshot() });
  assert.doesNotMatch(html, /data-action="learning-finalize"/);
});
