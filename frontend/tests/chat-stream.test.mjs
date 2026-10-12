import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { renderNoteMarkdown } from '../src/notes.js';
import { esc } from '../src/store.js';
import { renderAnswerMetadata } from '../src/study.js';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
function fixture(readMessage) {
  const space = { id: 's' }, state = { messages: new Map(), chats: new Map(), chatMaterials: new Map() };
  const context = vm.createContext({ state, esc, renderNoteMarkdown, renderAnswerMetadata, AbortController, DOMException,
    currentSpace: () => space, byId: () => space, data: () => ({ materials: [] }), $: () => null,
    updateChat: () => {}, toast: () => {},
    api: { sendMessage: async () => ({ run_id: 'r', session_id: 'session' }), readMessage } });
  vm.runInContext(app.slice(app.indexOf('function chatMessages()'), app.indexOf('function assistantPage(')), context);
  vm.runInContext(app.slice(app.indexOf('async function sendChat('), app.indexOf('function materialSpace(')), context);
  return { state, context };
}

test('space assistant displays incremental Markdown and replaces draft with one final answer', async () => {
  let options, finish;
  const { state, context } = fixture((_, value) => { options = value; return new Promise(resolve => { finish = resolve; }); });
  const sending = vm.runInContext("sendChat('问题')", context); await new Promise(resolve => setTimeout(resolve, 0));
  options.onDelta('正文', { text: '**流式正文**', provisional: true });
  assert.match(vm.runInContext('chatMessages()', context), /<strong>流式正文<\/strong>/);
  finish({ text: '最终正文', citations: [] }); await sending;
  assert.equal(state.chatDraft, null); assert.equal(state.messages.get('s').filter(m => m.role === 'assistant').length, 1);
  assert.doesNotMatch(vm.runInContext('chatMessages()', context), /流式正文/);
});

test('failed and cancelled streams cannot leave an unverified assistant bubble', async () => {
  let options, reject;
  const { state, context } = fixture((_, value) => { options = value; return new Promise((_, no) => { reject = no; }); });
  const sending = vm.runInContext("sendChat('问题')", context); await new Promise(resolve => setTimeout(resolve, 0));
  options.onDelta('正文', { text: '待核对内容', provisional: true });
  reject(Object.assign(new Error('失败'), { status: 400, code: 'RUN_FAILED' })); await sending;
  assert.equal(state.chatDraft, null); assert.equal(state.pendingJob, null);
  assert.equal(state.messages.get('s').filter(m => m.role === 'assistant').length, 0);
});

test('late citation lookup cannot switch the new node or read its document', async () => {
  let resolve, switches = 0, view = { space: { id: 'old' }, refs: [] };
  const context = vm.createContext({ location: { hash: '#/space/old/study' }, api: { source: () => new Promise(done => { resolve = done; }) },
    study: { snapshot: () => view, setMode: async () => { switches++; } }, $: () => null });
  vm.runInContext(app.slice(app.indexOf('async function readStudyCitation('), app.indexOf('function updateProgressPosition(')), context);
  const reading = vm.runInContext("readStudyCitation({material_id:'m',material_version_id:'v',chunk_id:'c'})", context);
  view = { space: { id: 'new' }, refs: [] }; resolve({ material_id: 'm', material_version_id: 'v', chunk_id: 'c' }); await reading;
  assert.equal(switches, 0); assert.equal(view.refs.length, 0);
});
