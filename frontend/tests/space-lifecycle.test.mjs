import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError, changeSpace } from '../src/api.js';
import { createLearningController } from '../src/learning.js';
import { storageWrite, removeSpaceData } from '../src/store.js';

const space = () => ({ id: 'space / 中文', name: '学习空间', status: 'active', space_version: 7 });
const response = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
const fixture = () => ({ spaces: [space(), { id: 's2', status: 'active', space_version: 1 }], materials: [{ id: 'shared-material' }], tasks: [{ id: 't1', space_id: space().id }, { id: 't2', space_id: 's2' }] });
const memory = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value) }; };

test('space mutations always require the backend to confirm the operation', async () => {
  let calls = 0;
  const selected = space();
  const api = { setSpaceStatus: async (snapshot, status) => { calls++; return { ...snapshot, status, space_version: 8 }; } };
  const result = await changeSpace({ api, space: selected, action: 'archive' });
  assert.equal(calls, 1);
  assert.equal(result.status, 'archived');
  assert.equal(selected.status, 'active');
});

test('space archive and deletion call versioned authenticated backend endpoints', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, ...options }); return response({ status: 'succeeded' }); });
  const api = createApi(() => 'fixture-token');
  await api.setSpaceStatus(space(), 'archived');
  await api.setSpaceStatus(space(), 'active');
  await api.deleteSpace(space());
  assert.deepEqual(calls.map(c => c.method), ['PATCH','PATCH','DELETE']);
  assert.deepEqual(calls.map(c => JSON.parse(c.body)), [
    { status: 'archived', expected_version: 7 }, { status: 'active', expected_version: 7 }, { confirm: true, expected_version: 7 },
  ]);
  for (const call of calls) {
    assert.equal(call.url, 'http://127.0.0.1:8000/api/v1/learning-spaces/' + encodeURIComponent(space().id));
    assert.equal(call.headers['X-Local-Token'], 'fixture-token');
    assert.equal(call.credentials, 'omit');
  }
});

test('deletion requires explicit confirmation and a valid captured version before any request', async () => {
  let calls = 0;
  const api = { deleteSpace: async () => { calls++; } };
  for (const confirmed of [false, undefined, 'true', 1]) {
    await assert.rejects(changeSpace({ api, mode: 'live', space: space(), action: 'delete', confirmed }), /确认/);
  }
  await assert.rejects(changeSpace({ api, mode: 'live', space: { ...space(), space_version: 0 }, action: 'delete', confirmed: true }), /刷新/);
  assert.equal(calls, 0);
});

test('live archive accepts only the matching updated version and status', async () => {
  const selected = space();
  const api = { setSpaceStatus: async (snapshot,status) => ({ ...snapshot, status, space_version: 8 }) };
  const updated = await changeSpace({ api, mode: 'live', space: selected, action: 'archive' });
  assert.equal(updated.status, 'archived');
  assert.equal(selected.status, 'active');
  api.setSpaceStatus = async () => ({ ...selected, status: 'archived' });
  await assert.rejects(changeSpace({ api, mode: 'live', space: selected, action: 'archive' }), /核对/);
});

test('delete removes only the selected space and its tasks, retaining shared materials and other spaces', async () => {
  const data = fixture(), originalMaterials = structuredClone(data.materials), remainingSpaces = structuredClone(data.spaces.slice(1));
  const selected = data.spaces[0];
  const api = { deleteSpace: async snapshot => ({ space_id: snapshot.id, status: 'succeeded', run_id: 'fixture-run' }) };
  await changeSpace({ api, mode: 'live', space: selected, action: 'delete', confirmed: true });
  removeSpaceData(data, selected.id);
  assert.deepEqual(data.spaces, remainingSpaces);
  assert.ok(data.tasks.every(task => task.space_id !== selected.id));
  assert.ok(data.tasks.some(task => task.space_id === data.spaces[0].id));
  assert.deepEqual(data.materials, originalMaterials);
  for (const remaining of [...data.spaces]) removeSpaceData(data, remaining.id);
  assert.deepEqual(data.spaces, []);
  assert.deepEqual(data.tasks, []);
  assert.deepEqual(data.materials, originalMaterials);
});

test('failed, mismatched and ambiguous delete responses never remove local records', async () => {
  const data = fixture(), selected = data.spaces[0], original = structuredClone(data);
  const outcomes = [
    { space_id: 'another-space', status: 'succeeded' }, { space_id: selected.id, status: 'failed' }, {},
    new ApiError('版本冲突，请刷新', 409, 'VERSION_CONFLICT'), new ApiError('连接中断', 0, 'NETWORK'),
  ];
  for (const outcome of outcomes) {
    let calls = 0;
    const api = { deleteSpace: async () => { calls++; if (outcome instanceof Error) throw outcome; return outcome; } };
    await assert.rejects(async () => {
      await changeSpace({ api, mode: 'live', space: selected, action: 'delete', confirmed: true });
      removeSpaceData(data, selected.id);
    });
    assert.equal(calls, 1);
    assert.deepEqual(data, original);
  }
});

test('queued deletion waits for successful completion before removing a space', async () => {
  const data = fixture(), selected = data.spaces[0];
  let finish;
  const completion = new Promise(resolve => { finish = resolve; });
  const api = { deleteSpace: async () => ({ space_id: selected.id, status: 'queued', run_id: 'fixture-run' }), waitRun: async id => { assert.equal(id, 'fixture-run'); await completion; } };
  const operation = changeSpace({ api, mode: 'live', space: selected, action: 'delete', confirmed: true }).then(() => removeSpaceData(data, selected.id));
  await Promise.resolve(); await Promise.resolve();
  assert.ok(data.spaces.some(item => item.id === selected.id));
  finish(); await operation;
  assert.ok(!data.spaces.some(item => item.id === selected.id));
});

test('deleting a space clears only its saved assessment and setup state', () => {
  const storage = memory();
  storageWrite(storage, 'knowpath-learning-assessments-v1', { s1: { assessmentId: 'a1', assessmentStatus: 'ready' }, s2: { assessmentId: 'a2' } });
  storageWrite(storage, 'knowpath-learning-setups-v1', { s1: { count: '8' }, s2: { count: '3' } });
  const controller = createLearningController({ api: {}, storage, context: () => ({ mode: 'live', spaceId: 's1' }) });
  assert.equal(controller.snapshot().assessmentId, 'a1');
  controller.forget('s1');
  assert.equal(controller.snapshot().assessmentId, '');
  assert.deepEqual(JSON.parse(storage.getItem('knowpath-learning-assessments-v1')), { s2: { assessmentId: 'a2' } });
  assert.deepEqual(JSON.parse(storage.getItem('knowpath-learning-setups-v1')), { s2: { count: '3' } });
});
