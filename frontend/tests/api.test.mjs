import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError } from '../src/api.js';

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
test('topic lookup uses frozen material versions and rejects inconsistent graph snapshots', async t => {
  const bindings = [{ material_id: 'm1', material_version_id: 'v1', graph_version: 3 }];
  let graphVersion = 3, captured;
  t.mock.method(globalThis, 'fetch', async url => { captured = url; return json({ material_id: 'm1', version_id: 'v1', graph_version: graphVersion, items: [{ id: 't1' }] }); });
  const api = createApi(() => '');
  assert.deepEqual(await api.topics({ bindings }), [{ id: 't1' }]);
  assert.ok(captured.endsWith('/materials/m1/topics?version_id=v1'));
  graphVersion = 4;
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

test('space graph reads each bound material projection once instead of each root topic', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => {
    calls.push(url);
    if (url.includes('/materials/m1/graph?')) return json({ material_id: 'm1', version_id: 'v1', graph_version: 2,
      nodes: [{ id: 'a', material_version_id: 'v1', graph_version: 2 }], edges: [], sources: [] });
    if (url.includes('/materials/m2/graph?')) return json({ material_id: 'm2', version_id: 'v2', graph_version: 3,
      nodes: [{ id: 'b', material_version_id: 'v2', graph_version: 3 }], edges: [], sources: [] });
    assert.fail(`unexpected graph request ${url}`);
  });
  const graph = await createApi(() => '').graph({ bindings: [
    { material_id: 'm1', material_version_id: 'v1', graph_version: 2 },
    { material_id: 'm2', material_version_id: 'v2', graph_version: 3 },
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
test('SSE reconnects to the same run with Last-Event-ID', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, cursor: options.headers['Last-Event-ID'] });
    return calls.length === 1 ? stream('id: 1\nevent: run.started\ndata: {}\n\n') : stream('id: 2\nevent: message.completed\ndata: {"text":"恢复成功","citations":[]}\n\nid: 3\nevent: run.completed\ndata: {}\n\n');
  });
  const result = await createApi(() => '').readMessage({ run_id: 'same-run' });
  assert.equal(result.text, '恢复成功'); assert.equal(calls.length, 2); assert.equal(calls[1].cursor, '1'); assert.equal(calls[0].url, calls[1].url);
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
