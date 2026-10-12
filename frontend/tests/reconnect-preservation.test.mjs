import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const extract = (from, to) => app.slice(app.indexOf(from), app.indexOf(to, app.indexOf(from)));

test('failed chat submission retains composer and does not invent a sent user message', async () => {
  const input = { value: '我的草稿' }, state = { messages: new Map(), chats: new Map(), chatMaterials: new Map() };
  const context = vm.createContext({ state, currentSpace: () => ({ id: 's' }), byId: () => ({ id: 's' }), $: () => input, updateChat() {}, toast() {}, AbortController, DOMException,
    api: { sendMessage: async () => { throw Object.assign(new Error('重启后端'), { code: 'BACKEND_RESTART_REQUIRED', status: 409 }); } } });
  vm.runInContext(extract('async function sendChat(', '\nfunction materialSpace('), context);
  await context.sendChat(input.value);
  assert.equal(input.value, '我的草稿'); assert.equal(state.messages.get('s')?.length || 0, 0); assert.equal(state.sending, false);
});

test('reconnect keeps an active reader mounted while refreshing authoritative data and route', async () => {
  const reader = { scrollTop: 240, iframe: {}, draft: '继续解释' }, states = [], calls = [];
  const context = vm.createContext({ token: 'local', session: {}, TOKEN_KEY: 'token', storageWrite() {}, AbortSignal,
    $: selector => selector === '.study-workspace' ? reader : null,
    state: { live: { spaces: [{ id: 's' }], materials: [] }, selected: 's', dataRefreshId: 0 },
    render() { states.push(context.state.loading); if (context.state.loading) reader.iframe = null; },
    api: { messageCapabilities: async () => ({ message: '' }), spaces: async () => { calls.push('spaces'); return [{ id: 's' }]; }, materials: async () => { calls.push('materials'); return []; } },
    workbench: { invalidate() { calls.push('invalidate'); } }, scheduleMaterialRefresh() {}, loadRoute: async () => { calls.push('route'); } });
  const iframe = reader.iframe;
  vm.runInContext(extract('async function connectBackend(', '\nasync function loadRoute('), context);
  await context.connectBackend();
  assert.ok(states.every(value => !value), 'active reader must never be replaced by a loading skeleton');
  assert.equal(reader.iframe, iframe); assert.equal(reader.scrollTop, 240); assert.equal(reader.draft, '继续解释');
  assert.deepEqual(calls, ['spaces', 'materials', 'invalidate', 'route']);
});

test('reconnect refresh of changed bound versions reloads learning context', async () => {
  const fresh = { id: 's', scope_version: 1, bindings: [{ material_id: 'm', material_version_id: 'new' }] };
  let options;
  const context = vm.createContext({ location: { hash: '#/space/s/study' }, route: () => ({ name: 'space', section: 'study', query: {} }), currentSpace: () => fresh,
    state: { chatCapabilityError: '' }, data: () => ({ materials: [] }),
    workbench: { snapshot: () => ({ data: { current_plan: { id: 'p' } } }) },
    study: { snapshot: () => ({ space: { ...fresh, bindings: [{ material_id: 'm', material_version_id: 'old' }] }, mode: 'original', refs: [] }), load: async (_, value) => { options = value; } } });
  vm.runInContext(extract('async function loadStudy(', '\nfunction updateStudy('), context);
  await context.loadStudy(); assert.equal(options.refresh, true);
});


test('reconnect render preserves connected study DOM rather than resetting main', () => {
  const reader = { iframe: {}, scrollTop: 360 }, calls = [];
  const context = vm.createContext({ location: { hash: '#/space/s/study' }, route: () => ({ name: 'space', section: 'study' }),
    state: { preserveStudyRoute: '#/space/s/study' }, $: selector => selector === '.study-workspace' ? reader : null,
    header() { calls.push('header'); }, updateStudy() { calls.push('study'); },
    main: { set innerHTML(_) { assert.fail('reconnect must keep reading DOM connected'); } } });
  vm.runInContext(extract('function render() {', 'const pendingMaterialStatuses'), context);
  const iframe = reader.iframe; context.render();
  assert.equal(reader.iframe, iframe); assert.equal(reader.scrollTop, 360); assert.deepEqual(calls, ['header', 'study']);
});
