import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError } from '../src/api.js';

test('plan ordering preserves encoded identity, preview body and apply key', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({url, options}); return json({ version: 2 }); });
  const api = createApi(() => 'local');
  const body = { expected_plan_version: 1, order_mode: 'custom', ordered_node_ids: ['n2', 'n1'] };
  await api.orderPreview('p/1', body);
  await api.reorderPlan('p/1', body, {key: 'stable-order'});
  assert.ok(calls[0].url.endsWith('/plans/p%2F1/order-preview'));
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), body);
  assert.ok(calls[1].url.endsWith('/plans/p%2F1/order'));
  assert.equal(calls[1].options.method, 'PATCH');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'stable-order');
});

test('run polling reports persisted stage before returning terminal result', async t => {
  t.mock.method(globalThis, 'fetch', async () => json({ id: 'r1', status: 'succeeded', assessment_stage: 'ready' }));
  const updates = [];
  const result = await createApi(() => 'local').waitRun('r1', undefined, run => updates.push(run));
  assert.equal(updates.length, 1);
  assert.equal(updates[0].assessment_stage, 'ready');
  assert.deepEqual(result, updates[0]);
});

test('study APIs preserve node linkage, request identity and audited history routes', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => { if (url.endsWith('/openapi.json')) return json(chatSchema()); calls.push({ url: new URL(url), options }); return new Response('{}', { headers: { 'Content-Type': 'application/json' } }); });
  const api = createApi(() => 'local');
  assert.equal(typeof api.generateStudyLesson, 'function');
  await api.studyLessons('s/a', 'n/b');
  await api.generateStudyLesson('s', 'n', { plan_id: 'p', task_id: 't', style: 'plain' }, { key: 'stable' });
  await api.studyLesson('lesson'); await api.retryStudyLesson('lesson', { key: 'retry' });
  await api.sendStudyMessage('s', { message: '解释', lesson_id: 'lesson', stream: true });
  await api.studyMessages('s', 'conversation', { cursor: 'next' });
  assert.ok(calls[0].url.pathname.endsWith('/s%2Fa/nodes/n%2Fb/lessons'));
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'stable');
  assert.equal(JSON.parse(calls[1].options.body).task_id, 't');
  assert.equal(JSON.parse(calls[4].options.body).lesson_id, 'lesson');
  assert.ok(calls[5].url.pathname.endsWith('/conversations/conversation/messages')); assert.equal(calls[5].url.searchParams.get('cursor'), 'next');
});

test('expired local authentication prompts automatic reconnection', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('{}', { status: 401 }));
  await assert.rejects(createApi(() => 'expired').health(), error => {
    assert.equal(error.status, 401);
    assert.match(error.message, /自动连接|重新连接/);
    assert.doesNotMatch(error.message, /输入|连接设置/);
    return true;
  });
});

const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
const chatSchema = (properties = { reading_context: {}, answer_mode: { enum: ['auto', 'material', 'general'] } }) => ({ paths: { '/api/v1/learning-spaces/{space_id}/messages': { post: { requestBody: { content: { 'application/json': { schema: { $ref: '#/components/schemas/SendMessage' } } } } } } }, components: { schemas: { SendMessage: { properties } } } });
test('message capability mismatch stops POST and instructs a backend restart', async t => {
  const calls = []; t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return json(chatSchema({ message: {}, lesson_id: {} })); });
  const api = createApi(() => 'local');
  await assert.rejects(api.sendStudyMessage('s', { message: '解释', reading_context: { material_id: 'm', material_version_id: 'v' } }), e => e.code === 'BACKEND_RESTART_REQUIRED' && /重启后端/.test(e.message));
  assert.equal(calls.length, 1); assert.equal(new URL(calls[0].url).pathname, '/openapi.json'); assert.equal(calls[0].options.method, 'GET');
});

test('new chat requests recheck the current backend and explicitly choose auto', async t => {
  const calls = []; t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return json(String(url).endsWith('/openapi.json') ? chatSchema() : {}); });
  const api = createApi(() => 'local'); await api.sendStudyMessage('s', { message: '解释' }); await api.sendMessage('s', '你好');
  assert.equal(calls.filter(c => c.url.endsWith('/openapi.json')).length, 2);
  assert.ok(calls.filter(c => c.options.method === 'POST').every(c => JSON.parse(c.options.body).answer_mode === 'auto'));
});

test('422 exposes field location and validation reason without echoing submitted content', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ error: { code: 'INVALID_REQUEST', message: '请求参数校验失败', details: { errors: [{ loc: ['body', 'reading_context'], type: 'extra_forbidden', msg: 'Extra inputs are not permitted' }] } } }), { status: 422 }));
  await assert.rejects(createApi(() => 'local').request('/test'), e => /reading_context/.test(e.message) && /Extra inputs/.test(e.message) && /extra_forbidden/.test(e.message));
});
test('SSE failure preserves the backend context invalidation code', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('id: 1\nevent: run.failed\ndata: {"error":{"code":"STALE_LEARNING_CONTEXT","message":"资料版本已变化"}}\n\n', { headers: { 'Content-Type': 'text/event-stream' } }));
  await assert.rejects(createApi(() => 'local').readMessage({ run_id: 'r' }), error => error.code === 'STALE_LEARNING_CONTEXT');
});
test('topic lookup uses frozen material versions and rejects inconsistent graph snapshots', async t => {
  const bindings = [{ material_id: 'm1', material_version_id: 'v1', graph_version: 1 }];
  let graphVersion = 1, captured;
  t.mock.method(globalThis, 'fetch', async url => { captured = url; return json({ material_id: 'm1', version_id: 'v1', graph_revision_id: null, graph_version: graphVersion, items: [{ id: 't1' }] }); });
  const api = createApi(() => '');
  assert.deepEqual(await api.topics({ bindings }), [{ id: 't1' }]);
  assert.equal(new URL(captured).searchParams.get('graph_version'), '1');
  assert.equal(new URL(captured).searchParams.get('legacy_snapshot'), 'true');
  graphVersion = 2;
  await assert.rejects(() => api.topics({ bindings }), error => error.code === 'STALE_KNOWLEDGE');
});
test('material graph loads the complete published projection in one request', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => {
    calls.push(url);
    return json({ material_id: 'm1', version_id: 'v1', graph_version: 1,
      nodes: [{ id: 't1', name: '第一章' }, { id: 't2', name: '第二章' }],
      edges: [{ id: 'e1', type: 'contains', from_id: 't1', to_id: 't2' }], sources: [] });
  });
  const graph = await createApi(() => '').materialGraph({ id: 'm1', current_version_id: 'v1' });
  assert.deepEqual(graph.nodes.map(node => node.id), ['t1', 't2']);
  assert.equal(graph.edges[0].id, 'e1');
  assert.equal(calls.length, 1);
  assert.ok(calls[0].endsWith('/materials/m1/graph?version_id=v1&include_sources=true'));
});

test('space topics and graph pin the same revision and reject mismatched response identity', async t => {
  const binding = { material_id: 'm/1', material_version_id: 'v/1', graph_version: 1, graph_revision_id: 'r/1' };
  const calls = []; let revision = binding.graph_revision_id, material = binding.material_id;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: new URL(url), options });
    return json({ material_id: material, version_id: binding.material_version_id, graph_version: 1,
      graph_revision_id: revision, items: [{ id: 'old' }], nodes: [{ id: 'old' }], edges: [] });
  });
  const api = createApi(() => ''), space = { bindings: [binding] };
  const controller = new AbortController();
  assert.deepEqual(await api.topics(space, { signal: controller.signal }), [{ id: 'old' }]);
  assert.deepEqual((await api.graph(space, { signal: controller.signal })).nodes, [{ id: 'old' }]);
  for (const call of calls) {
    assert.equal(call.url.searchParams.get('graph_revision_id'), binding.graph_revision_id);
    assert.equal(call.url.searchParams.get('version_id'), binding.material_version_id);
    assert.equal(call.url.searchParams.get('graph_version'), '1');
  }
  revision = 'new-revision';
  await assert.rejects(api.topics(space), e => e.code === 'STALE_KNOWLEDGE');
  await assert.rejects(api.graph(space), e => e.code === 'STALE_KNOWLEDGE');
  revision = binding.graph_revision_id; material = 'wrong-material';
  await assert.rejects(api.graph(space), e => e.code === 'STALE_KNOWLEDGE');
});

test('space graph and topics preserve cancellation options', async t => {
  const controller = new AbortController(); controller.abort();
  t.mock.method(globalThis, 'fetch', async (_, options) => {
    assert.equal(options.signal.aborted, true);
    throw new DOMException('Aborted', 'AbortError');
  });
  const api = createApi(() => ''), space = { bindings: [{ material_id: 'm', material_version_id: 'v', graph_version: 1 }] };
  for (const operation of ['topics', 'graph']) await assert.rejects(api[operation](space, { signal: controller.signal }), e => e.name === 'AbortError');
});

test('legacy space reads original projection and requires null revision identity', async t => {
  let revision = null; const calls = [];
  t.mock.method(globalThis, 'fetch', async url => {
    calls.push(new URL(url));
    return json({ material_id: 'm', version_id: 'v', graph_version: 1, graph_revision_id: revision,
      nodes: [{ id: 'original' }], edges: [], items: [{ id: 'original' }] });
  });
  const api = createApi(() => ''), space = { bindings: [{ material_id: 'm', material_version_id: 'v', graph_version: 1 }] };
  await api.topics(space); await api.graph(space);
  for (const url of calls) {
    assert.equal(url.searchParams.get('legacy_snapshot'), 'true');
    assert.equal(url.searchParams.get('graph_version'), '1');
    assert.equal(url.searchParams.get('graph_revision_id'), null);
  }
  for (revision of ['published-one', undefined]) {
    await assert.rejects(api.topics(space), e => e.code === 'STALE_KNOWLEDGE');
    await assert.rejects(api.graph(space), e => e.code === 'STALE_KNOWLEDGE');
  }
});

test('space graph reads each bound material projection once instead of each root topic', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => {
    calls.push(url);
    if (url.includes('/materials/m1/graph?')) return json({ material_id: 'm1', version_id: 'v1', graph_version: 2, graph_revision_id: 'r2',
      nodes: [{ id: 'a', material_version_id: 'v1', graph_version: 2 }], edges: [], sources: [] });
    if (url.includes('/materials/m2/graph?')) return json({ material_id: 'm2', version_id: 'v2', graph_version: 3, graph_revision_id: 'r3',
      nodes: [{ id: 'b', material_version_id: 'v2', graph_version: 3 }], edges: [], sources: [] });
    assert.fail(`unexpected graph request ${url}`);
  });
  const graph = await createApi(() => '').graph({ bindings: [
    { material_id: 'm1', material_version_id: 'v1', graph_version: 2, graph_revision_id: 'r2' },
    { material_id: 'm2', material_version_id: 'v2', graph_version: 3, graph_revision_id: 'r3' },
  ], topic_ids: ['a', 'b'] });
  assert.deepEqual(graph.nodes.map(node => node.id), ['a', 'b']);
  assert.equal(calls.length, 2);
  assert.ok(calls.every(url => url.includes('/graph?version_id=')));
});

test('material file request keeps the original PDF response and local auth', async t => {
  let captured;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    captured = { url: new URL(url), options };
    return new Response('%PDF-1.7', { headers: { 'Content-Type': 'application/pdf' } });
  });
  const response = await createApi(() => 'local-token').materialFile('m1', 'v1');
  assert.equal(captured.url.pathname, '/api/v1/materials/m1/versions/v1/file');
  assert.equal(captured.options.headers['X-Local-Token'], 'local-token');
  assert.equal(captured.options.credentials, 'omit');
  assert.equal(await response.text(), '%PDF-1.7');
});
test('polling preserves terminal run error code and task identity', async t => {
  const mock = t.mock.method(globalThis, 'fetch', async () => json({ status: 'failed', error: {
    code: 'QUESTION_VALIDATION_FAILED', message: 'Generated questions failed validation.', details: { cause: 'invalid' },
  } }));
  await assert.rejects(() => createApi(() => '').waitRun('r1'), error => {
    assert.equal(error.code, 'QUESTION_VALIDATION_FAILED');
    assert.equal(error.status, 200);
    assert.deepEqual(error.details, { cause: 'invalid', runId: 'r1', runStatus: 'failed' });
    return true;
  });
  mock.mock.mockImplementation(async () => json({ status: 'cancelled' }));
  await assert.rejects(() => createApi(() => '').waitRun('r2'), error => error.code === 'RUN_CANCELLED' && error.details.runStatus === 'cancelled');
});

const stream = text => new Response(new ReadableStream({ start(controller) {
  const bytes = new TextEncoder().encode(text);
  // Deliberately split inside Chinese UTF-8 code points and SSE boundaries.
  for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
  controller.close();
} }), { headers: { 'Content-Type': 'text/event-stream' } });

test('live writes use local auth, exact schema and idempotency keys', async t => {
  let captured;
  t.mock.method(globalThis, 'fetch', async (url, options) => { captured = { url, ...options }; return json({ id: 'space-1' }); });
  const api = createApi(() => 'test-token');
  await api.createSpace({ name: '空间', material_ids: ['m1'], weekly_minutes: 60 });
  assert.equal(captured.url, 'http://127.0.0.1:8000/api/v1/learning-spaces');
  assert.equal(captured.headers['X-Local-Token'], 'test-token');
  assert.ok(captured.headers['Idempotency-Key']);
  assert.equal(captured.credentials, 'omit');
  assert.deepEqual(JSON.parse(captured.body), { name: '空间', material_ids: ['m1'], weekly_minutes: 60 });
});
test('all cursor pages are loaded without silently dropping records', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async url => { calls++; return json(url.includes('cursor=') ? { items: [{ id: 'second' }], next_cursor: null } : { items: [{ id: 'first' }], next_cursor: 'cursor-one' }); });
  assert.deepEqual((await createApi(() => '').spaces()).map(x => x.id), ['first', 'second']); assert.equal(calls, 2);
});
test('authentication and network failures stay explicit', async t => {
  const mock = t.mock.method(globalThis, 'fetch', async () => new Response('{}', { status: 401 }));
  await assert.rejects(() => createApi(() => '').spaces(), error => error instanceof ApiError && error.status === 401);
  mock.mock.mockImplementation(async () => { throw new TypeError('Failed to fetch'); });
  await assert.rejects(() => createApi(() => '').spaces(), error => error.code === 'NETWORK');
});
test('SSE decodes chunked Unicode and returns cited answer', async t => {
  t.mock.method(globalThis, 'fetch', async () => stream('id: 1\r\nevent: message.completed\r\ndata: {"text":"线性变换保持加法和数乘。","citations":[{"chunk_id":"c1"}]}\r\n\r\nid: 2\nevent: run.completed\ndata: {}\n\n'));
  const job = { run_id: 'r1' }; const result = await createApi(() => '').readMessage(job);
  assert.equal(result.text, '线性变换保持加法和数乘。'); assert.equal(result.citations[0].chunk_id, 'c1'); assert.equal(job.lastEventId, '2');
});

test('SSE exposes incremental text and replaces provisional text with validated deltas', async t => {
  t.mock.method(globalThis, 'fetch', async () => stream('id: 1\nevent: message.delta\ndata: {"delta":"草稿", "provisional":true}\n\nid: 2\nevent: message.delta\ndata: {"delta":"正式"}\n\nid: 3\nevent: message.delta\ndata: {"delta":"回答"}\n\nid: 4\nevent: message.completed\ndata: {"text":"正式回答","citations":[]}\n\nid: 5\nevent: run.completed\ndata: {}\n\n'));
  const previews = []; await createApi(() => '').readMessage({ run_id: 'stream' }, { onDelta: (_, state) => previews.push(state.text) });
  assert.deepEqual(previews, ['草稿', '正式', '正式回答']);
});

test('SSE route metadata identifies model-only text before the final answer', async t => {
  t.mock.method(globalThis, 'fetch', async () => stream('id: 1\nevent: message.route\ndata: {"answer_mode":"general","supplement_explanation":"资料未覆盖"}\n\nid: 2\nevent: message.delta\ndata: {"delta":"解释", "provisional":true}\n\nid: 3\nevent: message.completed\ndata: {"text":"解释","citations":[],"answer_mode":"general","supplement_explanation":"资料未覆盖"}\n\nid: 4\nevent: run.completed\ndata: {}\n\n'));
  const previews = []; const result = await createApi(() => '').readMessage({ run_id: 'auto' }, { onDelta: (_, state) => previews.push(state) });
  assert.equal(previews[0].answer_mode, 'general'); assert.equal(previews[0].supplement_explanation, '资料未覆盖'); assert.equal(result.answer_mode, 'general');
});

test('lesson SSE delivers validated sections and finishes with a lesson reference', async t => {
  t.mock.method(globalThis, 'fetch', async () => stream('id: 1\nevent: lesson.section\ndata: {"lesson_id":"lesson","sections":[{"kind":"core","markdown":"要点"}]}\n\nid: 2\nevent: run.completed\ndata: {"result_ref":{"type":"study_lesson","id":"lesson"}}\n\n'));
  const received = []; const result = await createApi(() => '').readLessonRun({ run_id: 'lesson-run' }, { onEvent: (name, data) => received.push([name, data]) });
  assert.equal(result.result_ref.id, 'lesson'); assert.equal(received[0][0], 'lesson.section');
});
test('SSE reconnects to the same run with Last-Event-ID', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, cursor: options.headers['Last-Event-ID'] });
    return calls.length === 1 ? stream('id: 1\nevent: run.started\ndata: {}\n\n') : stream('id: 2\nevent: message.completed\ndata: {"text":"恢复成功","citations":[]}\n\nid: 3\nevent: run.completed\ndata: {}\n\n');
  });
  const result = await createApi(() => '').readMessage({ run_id: 'same-run' });
  assert.equal(result.text, '恢复成功'); assert.equal(calls.length, 2); assert.equal(calls[1].cursor, '1'); assert.equal(calls[0].url, calls[1].url);
});

test('same-run retry reset removes failed provisional prefix before new text', async t => {
  t.mock.method(globalThis, 'fetch', async () => stream('id: 1\nevent: message.delta\ndata: {"delta":"失败草稿","provisional":true}\n\nid: 2\nevent: message.reset\ndata: {}\n\nid: 3\nevent: message.delta\ndata: {"delta":"重试正文","provisional":true}\n\nid: 4\nevent: message.completed\ndata: {"text":"重试正文","citations":[]}\n\nid: 5\nevent: run.completed\ndata: {}\n\n'));
  const texts = [];
  await createApi(() => '').readMessage({ run_id: 'r' }, { onDelta: (_, value) => texts.push(value.text) });
  assert.deepEqual(texts, ['失败草稿', '', '重试正文']);
});
test('stopping a stream cancels its reader after headers have arrived', async t => {
  let cancelled = false, started;
  const ready = new Promise(resolve => { started = resolve; });
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({ start() { started(); }, cancel() { cancelled = true; } }), { headers: { 'Content-Type': 'text/event-stream' } }));
  const controller = new AbortController();
  const result = createApi(() => '').readMessage({ run_id: 'r1' }, { signal: controller.signal });
  await ready; await new Promise(resolve => setTimeout(resolve, 10)); controller.abort();
  await assert.rejects(result, error => error.name === 'AbortError'); assert.equal(cancelled, true);
});
test('task completion includes the current plan version', async t => {
  let captured; t.mock.method(globalThis, 'fetch', async (url, options) => { captured = { url, ...options }; return json({ status: 'completed', plan_version: 4 }); });
  await createApi(() => '').completeTask({ id: 'p1', version: 3 }, { id: 't1' });
  assert.equal(captured.method, 'PATCH'); assert.deepEqual(JSON.parse(captured.body), { status: 'completed', expected_plan_version: 3 });
});

test('task undo sends original version event reason and stable key through the public API', async t => {
  let captured;
  t.mock.method(globalThis,'fetch',async(url,options)=>{captured={url,...options};return json({status:'pending',plan_version:4});});
  const body={expected_plan_version:3,completion_event_id:'e1',reason:'mistake'};
  await createApi(()=>'').undoTaskCompletion('p/1','t/1',body,{key:'undo-key'});
  assert.match(captured.url,/plans\/p%2F1\/tasks\/t%2F1\/undo-completion$/);
  assert.equal(captured.method,'POST');assert.deepEqual(JSON.parse(captured.body),body);
  assert.equal(new Headers(captured.headers).get('Idempotency-Key'),'undo-key');
});
