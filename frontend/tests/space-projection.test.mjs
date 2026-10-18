import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import * as store from '../src/store.js';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const old = () => ({ id: 's', space_version: 4, scope_version: 2, topic_ids: ['old'], include_prerequisites: true, status: 'draft' });
const ack = () => ({ id: 's', space_version: 5, scope_version: 3, topic_ids: ['confirmed'], excluded_topic_ids: ['excluded'], include_prerequisites: false, scope_mode: 'custom' });
function environment() {
  let release;
  const context = vm.createContext({
    ...store,
    state: { dataRefreshId: 0, selected: 's', live: { spaces: [old(), { id: 'deleted', space_version: 8 }], materials: [] }, topics: new Map() },
    api: { spaces: () => new Promise(resolve => { release = resolve; }), materials: async () => [] },
    render() {}, scheduleMaterialRefresh() {}, loadRoute: async () => {},
    workbench: { invalidate() {}, snapshot: () => ({}) }, learning: { snapshot: () => ({}) },
  });
  context.byId = id => context.state.live.spaces.find(space => space.id === id);
  const start = app.indexOf('async function refreshData(');
  vm.runInContext(app.slice(start, app.indexOf('async function prepareAssessmentCreation(', start)), context);
  const committedStart = app.indexOf('  committed: async (target, content, change) => {');
  const committedEnd = app.indexOf('\n  savePdf:', committedStart);
  vm.runInContext('globalThis.committed = ({' + app.slice(committedStart, committedEnd) + '}).committed;', context);
  return { context, release: spaces => release(spaces) };
}
const snapshot = context => JSON.parse(JSON.stringify(context.state.live.spaces));

test('actual refreshData preserves a scope ACK received while its older list was in flight', async () => {
  const env = environment(), { context } = env;
  const flight = context.refreshData({ silent: true });
  Object.assign(context.state.live.spaces[0], ack());
  env.release([old(), { id: 'new', space_version: 1 }]);
  await flight;
  const spaces = snapshot(context);
  assert.equal(spaces[0].space_version, 5);
  assert.deepEqual(spaces[0].topic_ids, ['confirmed']);
  assert.equal(spaces[0].include_prerequisites, false);
  assert.deepEqual(spaces.map(space => space.id), ['s', 'new']);
});

test('actual management committed callback installs its ACK into main state before a stale list returns', async () => {
  const env = environment(), { context } = env;
  const flight = context.committed({ page: 'space-scope' }, { space: { ...old(), ...ack() } }, { spaceIds: ['s'], resources: ['spaces'] });
  assert.equal(context.state.live.spaces[0].space_version, 5);
  env.release([old(), { id: 'new', space_version: 1 }]);
  await flight;
  assert.equal(context.state.live.spaces[0].space_version, 5);
  assert.deepEqual(snapshot(context)[0].topic_ids, ['confirmed']);
  assert.deepEqual(snapshot(context).map(space => space.id), ['s', 'new']);
});

test('both actual list paths accept newer archive state and authoritative deletion membership', async () => {
  for (const management of [false, true]) {
    const env = environment(), { context } = env;
    Object.assign(context.state.live.spaces[0], ack());
    const flight = management
      ? context.committed({ page: 'space-scope' }, { space: { ...old(), ...ack() } }, { spaceIds: ['s'], resources: ['spaces'] })
      : context.refreshData({ silent: true });
    env.release([{ ...old(), space_version: 6, status: 'archived', topic_ids: ['server-new'] }]);
    await flight;
    assert.equal(context.state.live.spaces[0].space_version, 6);
    assert.equal(context.state.live.spaces[0].status, 'archived');
    assert.deepEqual(snapshot(context).map(space => space.id), ['s']);
    const deletion = management
      ? context.committed({ page: 'space-scope' }, {}, { spaceIds: ['s'], resources: ['spaces'] })
      : context.refreshData({ silent: true });
    env.release([]); await deletion;
    assert.deepEqual(snapshot(context), []);
    assert.equal(context.state.selected, '');
  }
});

test('actual main scope ACK updates current list row after a refresh replaced the original object', async () => {
  const env = environment(), { context } = env;
  let releaseAck;
  Object.assign(context, {
    saveScope: () => new Promise(resolve => { releaseAck = resolve; }),
    closeModal() {}, toast() {}, learning: { clear() {} },
    modalVersion: 1, scopePlanReturn: null,
  });
  context.state.plans = new Map();
  const start = app.indexOf('async function saveScopeForm(');
  const end = app.indexOf('\nasync function scopeModal(', start);
  vm.runInContext(app.slice(start, end) + '; globalThis.confirmScope = saveScopeForm;', context);
  const flight = context.confirmScope({ dataset: { space: 's' } });
  const original = context.state.live.spaces[0];
  context.state.live.spaces = [old()];
  releaseAck(ack()); await flight;
  assert.notEqual(context.state.live.spaces[0], original);
  assert.equal(context.state.live.spaces[0].space_version, 5);
  assert.deepEqual(snapshot(context)[0].topic_ids, ['confirmed']);

  const later = context.confirmScope({ dataset: { space: 's' } });
  Object.assign(context.state.live.spaces[0], { space_version: 6, topic_ids: ['newer'] });
  releaseAck(ack()); await later;
  assert.equal(context.state.live.spaces[0].space_version, 6);
  assert.deepEqual(snapshot(context)[0].topic_ids, ['newer']);
});

test('actual lesson preferences ACK cannot downgrade a newer confirmed scope', async () => {
  const space = { ...old(), ...ack() };
  const context = vm.createContext({ ...store, byId: () => space, toast() {},
    busy: async (_, action) => action(), FormData: class { get() { return ''; } },
    study: { preferences: async () => ({ ...old(), lesson_style: 'brief' }) },
  });
  const start = app.indexOf("  if (form.id === 'study-preferences') {");
  const end = app.indexOf("  if (form.id === 'study-question') {", start);
  vm.runInContext('globalThis.savePreferences = async form => {' + app.slice(start, end) + '};', context);
  await context.savePreferences({ id: 'study-preferences', querySelector() {} });
  assert.equal(space.space_version, 5);
  assert.deepEqual(space.topic_ids, ['confirmed']);
  assert.equal(space.include_prerequisites, false);
});
