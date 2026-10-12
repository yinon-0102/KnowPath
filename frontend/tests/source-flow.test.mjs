import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { esc, pdfKey, validatePdf, sourcePage, pdfLocation } from '../src/store.js';

// Exercise the actual app handlers with browser boundaries stubbed. This is
// a flow test, not a substitute for testing the browser's native PDF renderer.
const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
const handlers = app.slice(app.indexOf('function materialSpace('), app.indexOf("document.addEventListener('click'"));
const ref = { material_id: 'm1', material_version_id: 'v1', chunk_id: 'c1' };
const pdf = { blob: new Blob(['%PDF-1.7']), name: 'source.pdf', verified: true };
function environment({ source, cached = true, blocked = false } = {}) {
  const windows = [], notices = [], saved = [];
  const modal = { open: false, html: '' };
  const material = { id: 'm1', name: '来源.pdf', type: 'pdf', current_version_id: 'v1' };
  const space = { id: 's1', bindings: [{ material_id: 'm1', material_version_id: 'v1' }] };
  const cache = new Map(cached ? [[pdfKey('live', 'm1', 'v1'), pdf]] : []);
  const library = {
    peek: key => cache.get(key), get: async key => cache.get(key),
    save: async item => { saved.push(item); cache.set(item.key, { ...item, blob: item.file }); return true; },
  };
  const context = vm.createContext({
    esc, validatePdf, sourcePage, pdfLocation, pdfKey, modal, pdfLibrary: library, pdfUrls: new Set(), sourceReader: null, modalVersion: 0,
    state: { mode: 'live', graphSelected: 'topic-original' },
    data: () => ({ materials: [material], spaces: [space] }), currentSpace: () => space,
    normalizeType: item => item.type,
    api: {
      source: source || (async () => ({ text: '核对后的原文', page: 7 })),
      material: async () => ({ material, version: { id: 'v1', chunks: [{ text: '解析后的原文', page: 7 }] } }),
      materialFile: async () => ({ blob: async () => new Blob(['%PDF-1.7\nbackend-original'], { type: 'application/pdf' }) }),
      materialVersions: async () => [{ id: 'v1', content_hash: 'a'.repeat(64) }],
    },
    URL: { createObjectURL: () => 'blob:test-original', revokeObjectURL: () => {} }, setInterval: () => 1, clearInterval: () => {},
    window: { open: () => {
      if (blocked) return null;
      const popup = { closed: false, document: { body: {} }, destination: '', close() { this.closed = true; } };
      popup.location = { replace: url => { popup.destination = url; } };
      windows.push(popup); return popup;
    } },
    $: () => null, icon: () => '', button: (label, action) => `<button data-action="${action}">${label}</button>`, notice: text => text,
    toast: message => notices.push(message), showError: error => notices.push(error.message),
  });
  context.openModal = (title, content) => { context.modalVersion++; modal.open = true; modal.html = content; };
  context.closeModal = () => { context.modalVersion++; modal.open = false; };
  vm.runInContext(handlers, context);
  return { context, windows, notices, modal, cache, saved };
}

test('one source click opens the cached PDF at the resolved page only after the source check succeeds', async () => {
  let resolve, captured;
  const pending = new Promise(done => { resolve = done; });
  const env = environment({ source: (input, spaceId) => { captured = { input, spaceId }; return pending; } });
  const work = env.context.showSource(ref);
  assert.equal(env.windows.length, 1);
  assert.equal(env.windows[0].destination, '');
  assert.equal(captured.spaceId, 's1');
  assert.equal(captured.input.material_version_id, 'v1');
  resolve({ text: '第七页', page: 7 }); await work;
  assert.equal(env.windows[0].destination, 'blob:test-original#page=7&view=FitH');
  assert.equal(env.context.state.graphSelected, 'topic-original');
  assert.equal(env.modal.open, false);
});

test('failed source authorization closes the reserved tab and exposes a retry instead of a cached PDF', async () => {
  const env = environment({ source: async () => { throw new Error('来源不属于该空间'); } });
  await env.context.showSource(ref);
  assert.equal(env.windows[0].closed, true);
  assert.equal(env.windows[0].destination, '');
  assert.match(env.modal.html, /来源不属于该空间/);
  assert.match(env.modal.html, /重试/);
  assert.doesNotMatch(env.modal.html, /正在定位/);
});

test('closing the reader during loading prevents late navigation or reopened dialogs', async () => {
  let resolve;
  const env = environment({ source: () => new Promise(done => { resolve = done; }) });
  const work = env.context.showSource(ref);
  env.context.closeModal();
  resolve({ text: 'late text', page: 3 }); await work;
  assert.equal(env.windows[0].closed, true);
  assert.equal(env.windows[0].destination, '');
  assert.equal(env.modal.open, false);
});

test('missing PDF renders real source text and file selection, not a broken or guessed URL', async () => {
  const env = environment({ cached: false });
  await env.context.showSource(ref);
  assert.equal(env.windows.length, 0);
  assert.match(env.modal.html, /核对后的原文/);
  assert.match(env.modal.html, /选择原 PDF/);
  assert.doesNotMatch(env.modal.html, /blob:/);
});

test('material-library PDF view opens the backend original file when no browser cache exists', async () => {
  const env = environment({ cached: false });
  await env.context.openSourcePage({ material_id: 'm1', material_version_id: 'v1' }, { materialView: true });
  assert.equal(env.windows.length, 1);
  assert.equal(env.windows[0].destination, 'blob:test-original#page=7&view=FitH');
  assert.equal(env.modal.open, false);
});

test('reader mode retains text and graph review actions without automatically opening another tab', async () => {
  const env = environment();
  await env.context.showSource(ref, { materialView: true, forceReader: true });
  assert.equal(env.windows.length, 0);
  assert.match(env.modal.html, /审核知识图谱/);
  assert.match(env.modal.html, /更换 PDF/);
  assert.match(env.modal.html, /打开 PDF/);
});

test('blocked popups retain an explicit open button and useful source text', async () => {
  const env = environment({ blocked: true });
  await env.context.showSource(ref);
  assert.equal(env.modal.open, true);
  assert.match(env.modal.html, /浏览器阻止了自动打开/);
  assert.match(env.modal.html, /打开 PDF/);
  assert.match(env.modal.html, /核对后的原文/);
});

test('choosing the wrong original PDF never overwrites the saved association', async () => {
  const env = environment();
  await env.context.showSource(ref, { forceReader: true });
  await env.context.attachSourcePdf(new File(['%PDF-1.7\nwrong original'], 'wrong.pdf'));
  assert.equal(env.saved.length, 0);
  assert.equal(env.windows[0].closed, true);
  assert.equal(env.cache.get(pdfKey('live', 'm1', 'v1')), pdf);
  assert.match(env.notices.at(-1), /版本不一致/);
});


test('homepage keeps the learning-first route desk without duplicating space cards', () => {
  assert.match(app, /打开你的学习路线/);
  assert.match(app, /当前空间资料/);
  assert.match(app, /最近笔记/);
  assert.doesNotMatch(app, /你的学习，此刻继续。/);
  assert.match(app, /探索知识图谱/);
  assert.match(app, /class="home-summary"/);
});

test('live material imports schedule refresh while parsing', () => {
  assert.match(app, /scheduleMaterialRefresh/);
  assert.match(app, /clearMaterialRefresh/);
});


test('live material list exposes a direct delete action', () => {
  assert.match(app, /manageLink\('删除', 'material-delete'/);
  assert.match(app, /danger-link/);
  assert.match(styles, /material-action\{display:flex;grid-column:1\/-1/);
});

test('material refresh scheduler polls pending imports and stops after readiness', async () => {
  const scheduler = app.slice(app.indexOf('const pendingMaterialStatuses'), app.indexOf('async function refreshData'));
  let timer = null;
  const cleared = [];
  let refreshes = 0;
  const context = vm.createContext({
    state: { mode: 'live', live: { materials: [{ status: 'processing' }] }, materialRefreshTimer: null },
    setTimeout: (callback, delay) => { timer = { callback, delay }; return 7; },
    clearTimeout: id => { cleared.push(id); },
    refreshData: async ({ silent }) => {
      assert.equal(silent, true);
      refreshes += 1;
      context.state.live.materials = [{ status: 'ready' }];
    },
  });
  vm.runInContext(scheduler, context);
  context.scheduleMaterialRefresh();
  assert.equal(timer.delay, 2500);
  context.scheduleMaterialRefresh();
  assert.deepEqual(cleared, [7]);
  await timer.callback();
  assert.equal(refreshes, 1);
  assert.equal(context.state.materialRefreshTimer, null);
});
