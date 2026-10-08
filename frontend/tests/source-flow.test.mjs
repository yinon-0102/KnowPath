import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { esc, pdfKey, validatePdf, sourcePage, pdfLocation } from '../src/store.js';

// Exercise the actual app handlers with browser boundaries stubbed. This is
// a flow test, not a substitute for testing the browser's native PDF renderer.
const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
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
    api: { source: source || (async () => ({ text: '核对后的原文', page: 7 })), materialVersions: async () => [{ id: 'v1', content_hash: 'a'.repeat(64) }] },
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
