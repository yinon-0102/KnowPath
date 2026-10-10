import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { storageRead, storageWrite, TOKEN_KEY } from '../src/store.js';

const entries = await Promise.all([
  ['app', readFile(new URL('../src/app.js', import.meta.url), 'utf8')],
  ['learning', readFile(new URL('../src/learning.js', import.meta.url), 'utf8')],
  ['workbench', readFile(new URL('../src/workbench.js', import.meta.url), 'utf8')],
  ['store', readFile(new URL('../src/store.js', import.meta.url), 'utf8')],
].map(async ([key, value]) => [key, await value]));
const files = Object.fromEntries(entries);

test('frontend bootstraps automatic local auth and contains no demo connection controls', () => {
  assert.match(files.app, /__knowpath\/local-auth/);
  for (const forbidden of ['makeDemo', 'loadDemo', 'demo-mode', 'reset-demo', 'connection-form', 'local_token', 'DEMO_KEY', 'MODE_KEY']) {
    assert.doesNotMatch(files.app, new RegExp(forbidden.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
});

test('learning and workbench contain only backend-backed flows', () => {
  for (const forbidden of ['demoQuestion', 'demoQuestions', 'demoCurrentPlan', 'demoProgress', 'demoLearningRecord']) {
    assert.doesNotMatch(`${files.learning}\n${files.workbench}`, new RegExp(forbidden));
  }
  assert.doesNotMatch(files.store, /makeDemo|loadDemo|spaceProgress|DEMO_KEY|MODE_KEY/);
});

function bootstrap({ savedToken = '', auth, dataFailure } = {}) {
  const requests = [], saved = new Map(), renderings = [];
  const session = { getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value) };
  const context = vm.createContext({
    token: savedToken, session, storageRead, storageWrite, TOKEN_KEY, AbortSignal,
    state: { mode: 'live', loading: false, error: '', dataRefreshId: 0, selected: '', live: { spaces: [], materials: [], tasks: [] } },
    render: () => renderings.push(context.state.loading ? 'loading' : context.state.error ? 'error' : 'data'),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (auth instanceof Error) throw auth;
      return new Response(JSON.stringify(auth || { token: 'backend-token', api_base: 'http://127.0.0.1:8000/api/v1' }));
    },
    api: {
      spaces: async () => { requests.push({ token: context.token, resource: 'spaces' }); if (dataFailure) throw dataFailure; return [{ id: 'real-space' }]; },
      materials: async () => { requests.push({ token: context.token, resource: 'materials' }); return [{ id: 'real-material' }]; },
    },
    workbench: { invalidate() {} }, scheduleMaterialRefresh() {}, loadRoute: async () => {},
  });
  vm.runInContext(files.app.slice(files.app.indexOf('async function connectBackend'), files.app.indexOf('async function loadRoute')), context);
  return { context, requests, saved, renderings };
}

test('automatic auth finishes before loading actual backend records', async () => {
  const env = bootstrap();
  await env.context.connectBackend();
  assert.equal(env.requests[0].url, '/__knowpath/local-auth');
  assert.equal(env.requests[0].options.cache, 'no-store');
  assert.equal(env.requests[1].token, 'backend-token');
  assert.equal(env.requests[2].token, 'backend-token');
  assert.equal(env.context.state.live.spaces[0].id, 'real-space');
  assert.equal(env.context.state.live.materials[0].id, 'real-material');
  assert.equal(JSON.parse(env.saved.get(TOKEN_KEY)), 'backend-token');
  assert.equal(env.context.state.loading, false);
});

test('existing tab credential loads backend data without requesting a new credential', async () => {
  const env = bootstrap({ savedToken: 'tab-token' });
  await env.context.connectBackend();
  assert.equal(env.requests.length, 2);
  assert.equal(env.requests[0].token, 'tab-token');
});

test('failed automatic auth offers an error without loading or fabricating any records', async () => {
  const env = bootstrap({ auth: new Error('private backend-token information') });
  await env.context.connectBackend();
  assert.equal(env.requests.length, 1);
  assert.equal(env.context.state.live.spaces.length, 0);
  assert.equal(env.context.state.live.materials.length, 0);
  assert.match(env.context.state.error, /无法自动连接/);
  assert.doesNotMatch(env.context.state.error, /backend-token/);
});

test('invalid automatic auth payload never reaches the backend', async () => {
  for (const auth of [{ token: 'bad token' }, { token: '', api_base: 'http://127.0.0.1:8000/api/v1' }, { token: 'secret', api_base: 'https://foreign.example' }]) {
    const env = bootstrap({ auth }); await env.context.connectBackend();
    assert.equal(env.requests.length, 1);
    assert.match(env.context.state.error, /无法自动连接/);
  }
});

test('explicit reconnect rereads a rotated token and then retries real requests', async () => {
  const env = bootstrap({ savedToken: 'expired-token' });
  await env.context.connectBackend({ renew: true });
  assert.equal(env.requests[0].url, '/__knowpath/local-auth');
  assert.equal(env.requests[1].token, 'backend-token');
});

test('concurrent automatic connection attempts share one configuration request', async () => {
  const env = bootstrap();
  await Promise.all([env.context.connectBackend(), env.context.connectBackend()]);
  assert.equal(env.requests.filter(request => request.url).length, 1);
  assert.equal(env.requests.filter(request => request.resource === 'spaces').length, 1);
});

test('service health page includes a direct automatic reconnect action', () => {
  const healthPage = files.app.slice(files.app.indexOf('function managedPage()'), files.app.indexOf('async function openLearningRecord'));
  assert.match(healthPage, /button\('重新自动连接', 'reconnect'/);
});

test('a failed backend connection retains empty data and reports its error', async () => {
  const env = bootstrap({ dataFailure: new Error('后端服务未运行') });
  await env.context.connectBackend();
  assert.equal(env.context.state.error, '后端服务未运行');
  assert.equal(env.context.state.live.spaces.length, 0);
  assert.equal(env.context.state.loading, false);
});

test('complete application startup renders authenticated material data without reading local examples', async t => {
  const modules = await Promise.all(['icons', 'api', 'learning', 'workspace', 'workspace-view', 'progress', 'workbench', 'store', 'notes', 'study', 'plan-order', 'home-view'].map(name => import(`../src/${name}.js`)));
  const elements = Object.fromEntries(['main', 'header', 'footer', 'modal', 'toast'].map(id => [id, { innerHTML: '', open: false, addEventListener() {}, focus() {} }]));
  const document = { querySelector: selector => elements[selector.replace(/^#/, '')] || null, addEventListener() {} };
  const entries = new Map(), calls = [];
  const tabStorage = { getItem: key => entries.get(key), setItem: (key, value) => entries.set(key, value) };
  const context = vm.createContext({
    ...Object.assign({}, ...modules), document,
    window: { sessionStorage: tabStorage, addEventListener() {}, scrollTo() {} },
    location: { hash: '#/materials' }, crypto, URL, URLSearchParams, AbortController, AbortSignal, Blob, Response, setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url === '/__knowpath/local-auth') return new Response(JSON.stringify({ token: 'test-runtime-token', api_base: 'http://127.0.0.1:8000/api/v1' }));
      assert.equal(options.headers['X-Local-Token'], 'test-runtime-token');
      if (new URL(url).pathname === '/openapi.json') return new Response(JSON.stringify({ paths: { '/api/v1/learning-spaces/{space_id}/messages': { post: { requestBody: { content: { 'application/json': { schema: { $ref: '#/components/schemas/SendMessage' } } } } } } }, components: { schemas: { SendMessage: { properties: { reading_context: {}, answer_mode: { enum: ['auto', 'material', 'general'] } } } } } }));
      if (new URL(url).pathname.endsWith('/learning-spaces')) return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (new URL(url).pathname.endsWith('/materials')) return new Response(JSON.stringify({ items: [{ id: 'actual-material', name: '后端资料.txt', type: 'txt', status: 'ready' }], next_cursor: null }));
      if (new URL(url).pathname.endsWith('/notebooks')) return new Response(JSON.stringify({ items: [{ space_id: 'actual-space', name: '真实笔记册', directory: 'review-notes/actual-space/', chapter_count: 3, completed_count: 1, correction_count: 2 }] }));
      assert.fail(`Unexpected request ${url}`);
    },
  });
  t.mock.method(globalThis, 'fetch', context.fetch);
  Object.defineProperty(context.window, 'localStorage', { get() { assert.fail('Application must not read the old example store'); } });
  // Actual modules are injected above; dependency failure handling is exercised
  // separately by startup.test.mjs. Keep application globals in this VM scope.
  vm.runInContext(files.app.slice(files.app.indexOf('const $ =')).replace('void connectBackend();', 'startup = connectBackend();'), context);
  await context.startup;
  assert.match(elements.main.innerHTML, /后端资料.txt/);
  assert.match(elements.main.innerHTML, /manage\/material-delete\/actual-material/);
  assert.doesNotMatch(elements.main.innerHTML, /演示|示例|connection-form|test-runtime-token/);
  assert.equal(calls.length, 4);
  context.location.hash = '#/overview'; vm.runInContext('render()', context);
  assert.match(elements.main.innerHTML, /先创建空间并选择资料/);
  assert.doesNotMatch(elements.main.innerHTML, /演示|示例|test-runtime-token/);
  context.location.hash = '#/notes';
  await vm.runInContext('loadRoute()', context);
  assert.match(elements.main.innerHTML, /真实笔记册/);
  assert.match(elements.main.innerHTML, /1 \/ 3 个章节已完成/);
  assert.match(elements.main.innerHTML, /#\/space\/actual-space\/notes/);
});
