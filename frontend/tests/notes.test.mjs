import test from 'node:test';
import assert from 'node:assert/strict';
import * as notes from '../src/notes.js';
import { parseRoute } from '../src/workbench.js';
import { createApi, API_BASE } from '../src/api.js';
import { readFile } from 'node:fs/promises';

test('notes space route keeps chapter and revision', () => {
  assert.equal(parseRoute('#/space/s/notes?chapter=c&revision=v').section, 'notes');
});
test('notebook API methods use authenticated backend routes and optimistic version', async () => {
  const original = globalThis.fetch, seen = [];
  globalThis.fetch = async (url, options) => { seen.push({ url, options }); return { ok: true, status: 200, json: async () => ({ items: [] }) }; };
  try {
    const api = createApi(() => 'test');
    await api.notebooks(); await api.notebook('s/1'); await api.noteChapter('c');
    await api.updateNoteChapter('c', { expected_version: 4, blocks: [] });
    await api.noteRevisions('c'); await api.noteRevision('c', 'r'); await api.retryNoteGeneration('g', { key: 'retry-1' });
    assert.equal(seen[1].url, API_BASE + '/learning-spaces/s%2F1/notebook');
    assert.equal(seen[3].options.method, 'PATCH');
    assert.equal(JSON.parse(seen[3].options.body).expected_version, 4);
    assert.equal(seen[6].options.headers['Idempotency-Key'], 'retry-1');
    assert.ok(seen.every(r => r.options.headers['X-Local-Token'] === 'test'));
  } finally { globalThis.fetch = original; }
});
test('navigation, measurement results and progress records expose real notes', async () => {
  const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
  assert.match(app, /\['notes', '学习笔记'\]/); assert.match(app, /spaceTabButton\('notes'/);
  const learning = await readFile(new URL('../src/learning.js', import.meta.url), 'utf8');
  assert.match(learning, /state\.result\?\.notes\?\.chapter_id/);
});
test('Markdown escapes HTML and unsafe links while preserving code and safe links', () => {
  const html = notes.renderNoteMarkdown('# 标题\n<script>alert(1)</script>\n[x](javascript:alert)\n[来源](https://example.com)\n```java\nObject x = null;\n```');
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('href="javascript:'));
  assert.match(html, /<h1>标题<\/h1>/);
  assert.match(html, /<pre><code/);
  assert.match(html, /href="https:\/\/example.com"/);
});
test('generation refresh preserves draft and conflict save preserves user content', async () => {
  const chapter = { id: 'c', space_id: 's', version: 1, blocks: [{ id: 'b', kind: 'summary', markdown: 'original' }], generations: [] };
  const controller = notes.createNotesController({ api: { notebook: async () => ({ space_id: 's', chapters: [] }), noteChapter: async () => structuredClone(chapter), noteRevisions: async () => ({ items: [] }), updateNoteChapter: async () => { throw Object.assign(new Error('版本冲突'), { status: 409 }); } } });
  await controller.load({ name: 'space', id: 's', section: 'notes', query: { chapter: 'c' } });
  controller.edit('b', 'my edits');
  chapter.version = 2; chapter.blocks.push({ id: 'new', kind: 'summary', markdown: 'new result' });
  await controller.refresh();
  assert.equal(controller.snapshot().draft[0].markdown, 'my edits');
  await controller.save();
  assert.equal(controller.snapshot().draft[0].markdown, 'my edits');
  assert.match(controller.snapshot().error, /保留/);
  controller.mergeLatest();
  assert.equal(controller.snapshot().expectedVersion, 2);
  assert.equal(controller.snapshot().draft[0].markdown, 'my edits');
  assert.equal(controller.snapshot().draft[1].id, 'new');
  controller.dispose();
});
test('editing corrections submits only prose and preserves protected metadata on server', async () => {
  let submitted;
  const chapter = { id: 'c', space_id: 's', version: 1, blocks: [{ id: 'b', kind: 'assessment', markdown: '知识', source_refs: [{ chunk_id: 's' }] }], corrections: [{ id: 'q', markdown: '原理解', evidence_id: 'e' }] };
  const controller = notes.createNotesController({ api: { notebook: async () => ({ space_id: 's', chapters: [] }), noteChapter: async () => chapter, noteRevisions: async () => ({ items: [] }), updateNoteChapter: async (id, body) => { submitted = body; } } });
  await controller.load({ name: 'space', id: 's', section: 'notes', query: { chapter: 'c' } });
  controller.beginEdit(); controller.editCorrection('q', '已修改'); await controller.save();
  assert.deepEqual(submitted.corrections, [{ id: 'q', markdown: '已修改' }]);
  assert.deepEqual(submitted.blocks, [{ id: 'b', kind: 'assessment', markdown: '知识' }]);
  controller.dispose();
});
test('ambiguous retry reuses a stable idempotency key until acknowledged', async () => {
  const keys = [];
  const controller = notes.createNotesController({ api: { notebook: async () => ({ space_id: 's', chapters: [] }), retryNoteGeneration: async (_, options) => { keys.push(options?.key); throw new Error('network'); } } });
  await controller.load({ name: 'space', id: 's', section: 'notes', query: {} });
  await controller.retry('g'); await controller.retry('g');
  assert.ok(keys[0]); assert.equal(keys[0], keys[1]); controller.dispose();
});
test('only pending visible chapter is polled and dispose stops future requests', async () => {
  let calls = 0;
  const controller = notes.createNotesController({ delay: 15, api: { notebook: async () => ({ space_id: 's', chapters: [] }), noteChapter: async () => { calls++; return { id: 'c', space_id: 's', blocks: [], generations: [{ status: 'pending' }] }; }, noteRevisions: async () => ({ items: [] }) } });
  await controller.load({ name: 'space', id: 's', section: 'notes', query: { chapter: 'c' } });
  await new Promise(resolve => setTimeout(resolve, 40)); assert.ok(calls > 1);
  controller.dispose(); const stoppedAt = calls;
  await new Promise(resolve => setTimeout(resolve, 40)); assert.equal(calls, stoppedAt);
});
test('backend history rows keep chapter identity and failed generation exposes truthful structured draft', async () => {
  const controller = notes.createNotesController({ api: { notebook: async () => ({ space_id: 's', name: 'Space', chapters: [] }), noteRevision: async () => ({ id: 'rev', chapter_id: 'c', space_id: 's', version: 2, blocks: [] }), noteRevisions: async () => [{ id: 'rev', version: 2 }] } });
  await controller.load({ name: 'space', id: 's', section: 'notes', query: { chapter: 'c', revision: 'rev' } });
  assert.equal(controller.snapshot().chapter.id, 'c'); assert.equal(controller.snapshot().revisions.length, 1);
  controller.dispose();
  const html = notes.renderNotes({ notebook: { space_id: 's', name: 'Space', chapters: [] }, chapter: { id: 'c', space_id: 's', title: 'T', corrections: [{ id: 'q', markdown: '修改', confirmed: true }], generations: [{ id: 'g', status: 'failed', snapshot: { topic_ids: ['t'], learning_time: null, assessment_result: { question_results: [{ question_id: 'q', verdict: 'incorrect', score: 0 }] } } }] } });
  assert.match(html, /结构化草稿/); assert.match(html, /已确认纠偏/); assert.match(html, /复测/); assert.match(html, /未记录/);
});
test('space index is truthful and chapter renders corrections without treating unknown time as zero', () => {
  const html = notes.renderNotes({ notebook: { space_id: 's', name: 'Java', chapters: [{ id: 'c', number: '1.1', title: 'Object', status: 'IN_PROGRESS', correction_count: 0 }] }, chapter: { id: 'c', title: 'Object', space_id: 's', version: 1, status: 'IN_PROGRESS', blocks: [{ id: 'b', kind: 'summary', markdown: '核心知识', corrections: [{ id: 'q', explanation: '纠正理解' }] }], generations: [{ id: 'g', status: 'failed' }] }, revisions: [], draft: null });
  assert.match(html, /进行中/); assert.match(html, /纠正理解/); assert.match(html, /未记录/); assert.match(html, /notes-retry/);
  const index = notes.renderNotes({ notebook: { space_id: 's', name: 'Space', chapters: [{ id: 'c', status: 'IN_PROGRESS', generation_status: '已生成' }] } });
  assert.match(index, /已生成/);
});
test('invalidated automatic content is clearly historical evidence rather than current verified knowledge', () => {
  const html = notes.renderNotes({ notebook: { space_id: 's', name: 'Space', chapters: [] }, chapter: { id: 'c', space_id: 's', title: 'T', error: 'MODEL_UNAVAILABLE', blocks: [{ id: 'b', kind: 'assessment', markdown: '旧知识', evidence_validity: 'stale' }] } });
  assert.match(html, /依据已失效/); assert.match(html, /当前已验证/); assert.match(html, /MODEL_UNAVAILABLE/);
  assert.ok(html.indexOf('依据已失效') < html.indexOf('旧知识'));
});
