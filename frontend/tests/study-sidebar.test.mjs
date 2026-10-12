import test from 'node:test';
import assert from 'node:assert/strict';
import { createStudyController, renderStudy, renderAnswerMetadata } from '../src/study.js';

const refs = [{ material_id: 'm1', material_version_id: 'v1', chunk_id: 'c1', material_name: '资料一.md' }, { material_id: 'm2', material_version_id: 'v2', chunk_id: 'c2', material_name: '资料二.md' }];
const space = { id: 's', status: 'active', scope_version: 1, bindings: refs.map(r => ({ material_id: r.material_id, material_version_id: r.material_version_id })) };
const plan = { id: 'p', status: 'ready', tasks: [{ id: 't', node_id: 'n', status: 'pending', title: '节点', context: { topics: [{ id: 'topic', source_refs: refs }] } }] };
const memory = () => { const values = new Map(); return { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value) }; };
const api = overrides => ({ learningRecord: async () => ({ space_id: 's', topics: plan.tasks[0].context.topics }), studyLessons: async () => ({ items: [] }),
  source: async r => ({ ...r, text: '原文' }), materialFile: async () => new Response('# 原文', { headers: { 'Content-Type': 'text/markdown' } }),
  studyMessages: async () => ({ items: [] }), ...overrides });

test('original question works without a lecture and freezes current document, using an independent conversation', async () => {
  let sent;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async (_, body) => { sent = body; return { run_id: 'r', session_id: 'chat' }; }, readMessage: async () => ({ text: '原文回答' }) }) });
  await c.load({ space, plan, activeSession: { session_id: 'learning', task_id: 't', status: 'active' } }); await c.readSource(0); await c.ask('解释文档另一章');
  assert.deepEqual(sent.reading_context, { material_id: 'm1', material_version_id: 'v1' });
  assert.equal(sent.lesson_id, undefined); assert.equal(sent.session_id, undefined); c.dispose();
});

test('documents and lecture versions retain separate questions and conversation ids across reload', async () => {
  const storage = memory(), sent = [], versions = [1, 2].map(version => ({ id: 'l' + version, version, status: 'completed', style: 'plain', instructions: '' }));
  const client = api({ studyLessons: async () => ({ items: versions }), studyLesson: async id => ({ ...versions.find(v => v.id === id), sections: [] }),
    sendStudyMessage: async (_, body) => { sent.push(body); return { run_id: 'r' + sent.length, session_id: 'chat' + sent.length }; }, readMessage: async () => ({ text: '回答' }) });
  const c = createStudyController({ storage, api: client }); await c.load({ space, plan }); await c.readSource(0); c.question('第一份草稿'); await c.ask('第一份');
  await c.readSource(1); assert.equal(c.snapshot().questionDraft, ''); await c.ask('第二份');
  await c.readSource(0); assert.equal(c.snapshot().chatSession, 'chat1');
  c.question('原文未提交'); await c.setMode('explanation'); await c.readLesson('l1'); c.question('讲义一草稿'); await c.ask('讲义一');
  await c.readLesson('l2'); assert.equal(c.snapshot().chatSession, undefined); await c.ask('讲义二');
  await c.readLesson('l1'); assert.equal(c.snapshot().chatSession, 'chat3');
  await c.setMode('original', { sourceIndex: 0 }); assert.equal(c.snapshot().questionDraft, '原文未提交');
  c.dispose(); const restored = createStudyController({ storage, api: client }); await restored.load({ space, plan }); await restored.readSource(0);
  assert.equal(restored.snapshot().chatSession, 'chat1'); assert.equal(restored.snapshot().questionDraft, '原文未提交');
  assert.ok(sent.every(body => !body.session_id)); restored.dispose();
});

test('context switching aborts subscription but preserves the run and fences late deltas', async () => {
  let options;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => ({ run_id: 'r', session_id: 'chat' }),
    readMessage: (_, value) => { options = value; return new Promise((_, reject) => { value.signal.addEventListener('abort', () => reject(new DOMException('停止接收', 'AbortError'))); }); } }) });
  await c.load({ space, plan }); await c.readSource(0); const pending = c.ask('问题'); await new Promise(resolve => setTimeout(resolve, 0));
  await c.readSource(1); await assert.rejects(pending, error => error.name === 'AbortError');
  options.onDelta('旧回答', { text: '旧回答', provisional: true }); assert.ok(!c.snapshot().chatDraft); assert.ok(!c.snapshot().pendingChat);
  await c.readSource(0); assert.equal(c.snapshot().pendingChat.run_id, 'r'); c.dispose();
});

test('sidebar identifies original context and exposes responsive controls without a bottom follow-up', async () => {
  const c = createStudyController({ storage: memory(), api: api() }); await c.load({ space, plan }); await c.readSource(0);
  const html = renderStudy(c.snapshot()); assert.match(html, /data-study-sidebar/); assert.match(html, /资料一\.md/);
  assert.match(html, /data-study-action="sidebar-open"/); assert.doesNotMatch(html, /class="panel study-followup"/);
  assert.doesNotMatch(html, /id="study-question-input"[^>]+disabled/);
  c.sidebar(false); assert.equal(c.snapshot().sidebarOpen, false); c.question('保留草稿'); c.sidebar(true); assert.equal(c.snapshot().questionDraft, '保留草稿'); c.dispose();
});

test('a delayed submission acknowledgment remains owned by its original document', async () => {
  let accept;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: () => new Promise(resolve => { accept = resolve; }),
    readMessage: () => assert.fail('a detached context must not subscribe') }) });
  await c.load({ space, plan }); await c.readSource(0); const pending = c.ask('原文一问题');
  await c.readSource(1); c.question('原文二草稿'); accept({ run_id: 'delayed', session_id: 'first-chat' }); await pending;
  assert.equal(c.snapshot().questionDraft, '原文二草稿'); assert.equal(c.snapshot().pendingChat, undefined);
  await c.readSource(0); assert.equal(c.snapshot().pendingChat.run_id, 'delayed'); assert.equal(c.snapshot().chatSession, 'first-chat'); c.dispose();
});

test('history carrying another document identity is rejected before its answer is exposed', async () => {
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => ({ run_id: 'r', session_id: 'chat' }),
    readMessage: async () => ({}), studyMessages: async () => ({ items: [{ id: 'foreign', reading_context: { material_id: 'm2', material_version_id: 'v2' }, response: { text: '外文档回答' } }] }) }) });
  await c.load({ space, plan }); await c.readSource(0); await assert.rejects(c.ask('问题'), /阅读依据不一致/);
  assert.deepEqual(c.snapshot().messages, []); c.dispose();
});

test('same run resume starts at zero for assistance auditing and retains its identity', async () => {
  let received;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => ({ run_id: 'same-run', session_id: 'chat' }),
    readMessage: async job => { received = { ...job }; throw Object.assign(new Error('断线'), { code: 'RUN_PENDING' }); } }) });
  await c.load({ space, plan }); await c.readSource(0); await assert.rejects(c.ask('问题'), /断线/);
  c.snapshot().chat.pendingChat.lastEventId = '50'; c.snapshot().chat.pendingChat.streamText = '未重新审计正文';
  await assert.rejects(c.ask('', true), /断线/); assert.equal(received.run_id, 'same-run'); assert.equal(received.lastEventId, '0'); assert.equal(received.streamText, ''); c.dispose();
});

test('deleted source clears cached answers and input when the reading request fails', async () => {
  let deleted = false;
  const client = api({ materialFile: async () => { if (deleted) throw Object.assign(new Error('资料已删除'), { status: 404 }); return new Response('# 原文'); } });
  const c = createStudyController({ storage: memory(), api: client }); await c.load({ space, plan }); await c.readSource(0);
  c.snapshot().chat.messages = [{ id: 'old', response: { text: '已删除资料回答' } }]; c.question('旧资料草稿'); deleted = true;
  await assert.rejects(c.readSource(0), /资料已删除/); assert.deepEqual(c.snapshot().messages, []); assert.equal(c.snapshot().questionDraft, ''); assert.match(c.snapshot().readerError, /资料已删除/); c.dispose();
});

test('aborted old receiver cleanup cannot erase a newly resumed receiver in the same context', async () => {
  const receivers = [];
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => ({ run_id: 'r', session_id: 'chat' }),
    readMessage: (_, options) => new Promise((resolve, reject) => receivers.push({ options, resolve, reject })) }) });
  await c.load({ space, plan }); await c.readSource(0); const old = c.ask('问题'); await new Promise(resolve => setTimeout(resolve, 0));
  await c.readSource(1); await c.readSource(0); const next = c.ask('', true); await new Promise(resolve => setTimeout(resolve, 0));
  receivers[1].options.onDelta('', { text: '新接收正文', provisional: true });
  receivers[0].reject(new DOMException('旧接收已中止', 'AbortError')); await assert.rejects(old, error => error.name === 'AbortError');
  assert.equal(c.snapshot().sending, true); assert.equal(c.snapshot().chatDraft.text, '新接收正文');
  receivers[1].resolve({}); await next; c.dispose();
});

test('a stale context reported by the stream removes all cached prose and input', async () => {
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => ({ run_id: 'r', session_id: 'chat' }),
    readMessage: async (_, options) => { options.onDelta('', { text: '即将失效', provisional: true }); throw Object.assign(new Error('范围已变化'), { code: 'STALE_LEARNING_CONTEXT', status: 409 }); } }) });
  await c.load({ space, plan }); await c.readSource(0); c.snapshot().chat.messages = [{ id: 'old', response: { text: '旧内容' } }];
  await assert.rejects(c.ask('问题'), /范围已变化/); assert.deepEqual(c.snapshot().messages, []); assert.equal(c.snapshot().chatDraft, null); assert.equal(c.snapshot().questionDraft, ''); assert.match(c.snapshot().chatError, /更新阅读依据/); c.dispose();
});

test('restoring a selected source and a node-specific conversation never changes its identity', async () => {
  const storage = memory(), client = api(); const c = createStudyController({ storage, api: client });
  await c.load({ space, plan }); await c.readSource(1); c.question('第二文档草稿'); const firstKey = c.snapshot().chatContextKey; c.dispose();
  const restored = createStudyController({ storage, api: client }); await restored.load({ space, plan }); await restored.readSource();
  assert.equal(restored.snapshot().readingRef.material_id, 'm2'); assert.equal(restored.snapshot().questionDraft, '第二文档草稿');
  await restored.load({ space, plan: { ...plan, tasks: [{ ...plan.tasks[0], id: 't2', node_id: 'n2' }] } }); await restored.readSource(1);
  assert.notEqual(restored.snapshot().chatContextKey, firstKey); assert.equal(restored.snapshot().questionDraft, ''); restored.dispose();
});

test('a failed detached submission does not leave its origin permanently submitting', async () => {
  let rejectPost;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: () => new Promise((_, reject) => { rejectPost = reject; }) }) });
  await c.load({ space, plan }); await c.readSource(0); const pending = c.ask('问题'); await c.readSource(1);
  rejectPost(new Error('网络失败')); await assert.rejects(pending, /网络失败/); await c.readSource(0);
  assert.equal(c.snapshot().chatSubmitting, false); c.dispose();
});

test('late pagination cannot restore answers erased by source invalidation', async () => {
  let acceptHistory, deleted = false;
  const c = createStudyController({ storage: memory(), api: api({ studyMessages: () => new Promise(resolve => { acceptHistory = resolve; }),
    materialFile: async () => { if (deleted) throw Object.assign(new Error('已删除'), { status: 404 }); return new Response('# 原文'); } }) });
  await c.load({ space, plan }); await c.readSource(0); c.snapshot().chat.chatSession = 'chat'; c.snapshot().chat.historyLoaded = true;
  const pagination = c.moreHistory(); deleted = true; await assert.rejects(c.readSource(0), /已删除/);
  acceptHistory({ items: [{ id: 'old', reading_context: { material_id: 'm1', material_version_id: 'v1' }, response: { text: '删除内容' } }] }); await pagination;
  assert.deepEqual(c.snapshot().messages, []); c.dispose();
});

test('source retry and parsed fallback target the selected document rather than the first one', async () => {
  const c = createStudyController({ storage: memory(), api: api({ materialFile: async id => { if (id === 'm2') throw new Error('原文不可用'); return new Response('# 原文'); } }) });
  await c.load({ space, plan }); await assert.rejects(c.readSource(1), /原文不可用/);
  const html = renderStudy(c.snapshot()); assert.match(html, /data-study-action="source" data-index="1"[^>]*>重试原文阅读/);
  assert.match(html, /data-study-action="parsed-source" data-index="1"/); c.dispose();
});

test('answers distinguish source-backed text and model supplementation', () => {
  assert.match(renderAnswerMetadata({ answer_mode: 'general' }), /模型回答/);
  assert.match(renderAnswerMetadata({ answer_mode: 'material' }), /资料回答/);
  const mixed = renderAnswerMetadata({ answer_mode: 'mixed', supplement_explanation: '资料只覆盖部分内容' });
  assert.match(mixed, /资料回答.*模型补充/); assert.match(mixed, /资料只覆盖部分内容/);
});

test('outdated backend disables submission but leaves original reading and draft intact', async () => {
  let sent = false;
  const c = createStudyController({ storage: memory(), api: api({ sendStudyMessage: async () => { sent = true; } }) });
  await c.load({ space, plan, capabilityError: '请重启后端 API' }); await c.readSource(0); c.question('保留的问题');
  await assert.rejects(c.ask('保留的问题'), /重启后端/); assert.equal(sent, false); assert.equal(c.snapshot().questionDraft, '保留的问题');
  assert.ok(c.snapshot().reader); assert.match(renderStudy(c.snapshot()), /请重启后端 API/); c.dispose();
});
