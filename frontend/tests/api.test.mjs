import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError } from '../src/api.js';

const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
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
