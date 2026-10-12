import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const source = app.slice(app.indexOf('async function createPlan(form)'), app.indexOf('\nasync function completeTask'));

test('actual rolling plan form sends the strict backend calendar timezone field', async () => {
  let request;
  const values = new Map([['schedule_mode', 'budget_rolling'], ['include_review', 'on'],
    ['daily_minutes', '15'], ['horizon_days', '7'], ['calendar_timezone', 'UTC'], ['timezone', 'UTC']]);
  const fn = new Function('byId', 'state', 'FormData', 'planSettings', '$', 'api', source + '; return createPlan;')(
    () => ({ id: 'space' }), { plans: new Map() }, class { constructor() { return values; } },
    () => ({ sessions: { valid: true, value: 3 }, minutes: { valid: true, value: 25 } }),
    () => null, { createPlan: async (_, payload) => { request = payload; throw Error('CAPTURED'); } });
  await assert.rejects(fn({ dataset: { space: 'space' }, querySelectorAll: () => [] }), /CAPTURED/);
  assert.equal(request.calendar_timezone, 'UTC');
  assert.equal(request.daily_minutes, 15);
  assert.equal(request.horizon_days, 7);
  assert.ok(!('timezone' in request));
  assert.ok(!('session_count' in request));
});
