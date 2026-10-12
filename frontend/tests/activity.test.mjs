import test from 'node:test';
import assert from 'node:assert/strict';
let createActivityController;
try { ({ createActivityController } = await import('../src/activity.js')); } catch (e) { if (e.code !== 'ERR_MODULE_NOT_FOUND') throw e; }
const memory = () => { const m = new Map(); return { getItem: k => m.get(k), setItem: (k,v) => m.set(k,v) }; };
test('manual pause survives visibility and refresh without persisting elapsed time', async () => {
  assert.equal(typeof createActivityController, 'function');
  const storage = memory(), events = [];
  const send = async body => { events.push(body); return { activity: { active_seconds: 12 }, paused: true }; };
  const c = createActivityController({ send, storage }); await c.attach('session:s'); await c.event('pause'); await c.event('hidden'); await c.event('visible');
  assert.equal(c.snapshot().manualPaused, true); c.dispose();
  const next = createActivityController({ send, storage }); await next.attach('session:s'); assert.equal(next.snapshot().manualPaused, true);
  assert.equal(next.snapshot().activity.active_seconds, 12); assert.ok(!JSON.stringify(storage.getItem('knowpath-activity:session:s')).includes('active_seconds')); next.dispose();
});
test('heartbeat uses 30 second interval and does not turn elapsed into client truth', async () => {
  assert.equal(typeof createActivityController, 'function');
  let tick, interval; const events = [];
  const c = createActivityController({ send: async body => { events.push(body.event); return { paused: false, activity: { active_seconds: 20 } }; },
    setInterval: (fn, ms) => { tick = fn; interval = ms; return { unref() {} }; }, clearInterval() {} });
  await c.attach('assessment:a:q', { question_id: 'q' }, 'shown'); assert.equal(interval, 30000); await tick();
  assert.deepEqual(events, ['shown', 'heartbeat']); assert.equal(c.snapshot().activity.active_seconds, 20); c.dispose();
});
test('idle reading only requests a check and interaction clears it without server timing claims', async () => {
  let now = 0, tick; const listeners = new Map(), events = [];
  const document = { visibilityState: 'visible', addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) };
  const c = createActivityController({ document, now: () => now, send: async body => { events.push(body); return { paused: false }; },
    setInterval: fn => { tick = fn; return 1; }, clearInterval() {} });
  await c.attach('s'); now = 900000; await tick(); assert.equal(c.snapshot().needsActivityCheck, true); assert.equal(c.snapshot().paused, false);
  assert.ok(events.every(body => Object.keys(body).every(key => key === 'event')));
  listeners.get('keydown')(); assert.equal(c.snapshot().needsActivityCheck, false);
  await c.event('pause'); now += 900000; await tick(); assert.equal(c.snapshot().needsActivityCheck, false); await c.event('hidden'); await c.event('visible'); assert.equal(c.snapshot().manualPaused, true); c.dispose();
});
test('disposing an exposed question sends disconnect before identity is detached', async () => {
  const events = []; const c = createActivityController({ send: async body => events.push(body) });
  await c.attach('assessment:a:q', { question_id: 'q' }, 'shown'); await c.dispose();
  assert.deepEqual(events, [{ question_id: 'q', event: 'shown' }, { question_id: 'q', event: 'disconnect' }]); assert.equal(c.snapshot().identity, '');
});
