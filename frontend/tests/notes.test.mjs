import test from 'node:test';
import assert from 'node:assert/strict';
import * as notes from '../src/notes.js';
import { parseRoute } from '../src/workbench.js';
import { createApi, API_BASE } from '../src/api.js';
import { readFile } from 'node:fs/promises';

test('notes space route keeps chapter and revision', () => {
  assert.equal(parseRoute('#/space/s/notes?chapter=c&revision=v').section, 'notes');
});

const notebookWithGeneratedChapter = () => ({
  space_id: 's', name: 'Python 入门', directory: 'review-notes/python-s/',
  chapters: [{ id: 'c', number: '1.1', title: '3. 循环、range 与列表', filename: '1.1-3. 循环、range 与列表.md', status: 'IN_PROGRESS', generation_status: '已生成' }],
  readme_markdown: '# Python 入门\n\n| 章节 | 状态 |\n| --- | --- |\n| [1.1 3. 循环、range 与列表](1.1-3. 循环、range 与列表.md) | 进行中 |',
});

test('chapter distinguishes generated text from task confirmation and mastery conditions', () => {
  const book = notebookWithGeneratedChapter();
  const chapter = {...book.chapters[0], space_id:'s',blocks:[],generations:[],completion_conditions:{task_confirmed:false,all_topics_mastered:false,topics:[]}};
  const html=notes.renderNotes({notebook:book,chapter});
  assert.match(html,/笔记生成：已生成/);assert.match(html,/任务确认：尚未确认/);assert.match(html,/掌握条件：尚未满足/);
});

test('README links with Chinese filenames and spaces open the cloud chapter rather than a local file', () => {
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter() });
  const readme = html.slice(html.indexOf('<summary>查看 README</summary>'));
  assert.match(readme, /<a href="#\/space\/s\/notes\?chapter=c"[^>]*>1\.1 3\. 循环、range 与列表<\/a>/);
  assert.doesNotMatch(readme, /\[1\.1 3\./);
  assert.doesNotMatch(readme, /href="1\.1-/);
});

test('generated chapters have an explicit reading action even while mastery remains in progress', () => {
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter() });
  assert.match(html, /<a[^>]+href="#\/space\/s\/notes\?chapter=c"[^>]*>查看笔记<\/a>/);
  assert.match(html, /进行中/);
});

test('opening a chapter displays generated body before the notebook index and exposes the selected chapter', () => {
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter(), chapter: {
    id: 'c', space_id: 's', number: '1.1', title: '3. 循环、range 与列表', status: 'IN_PROGRESS', generation_status: '已生成',
    blocks: [{ id: 'b', kind: 'assessment', markdown: '## 核心知识\n\n循环用于重复执行。' }],
  } });
  assert.ok(html.indexOf('循环用于重复执行。') < html.indexOf('notes-index'), 'the chapter must not be buried below the README index');
  assert.match(html, /aria-current="page"/);
  assert.match(html, /返回笔记索引/);
});

test('README does not make unrelated relative files or executable addresses into chapter links', () => {
  const book = notebookWithGeneratedChapter();
  book.readme_markdown += '\n\n[未知资料](other.md)\n[危险](javascript:alert)\n[外部来源](https://example.com)';
  const html = notes.renderNotes({ notebook: book });
  assert.doesNotMatch(html, /href="(?:other\.md|javascript:)/);
  assert.match(html, /href="https:\/\/example.com"/);
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

test('Markdown preserves ordered steps, quotes and tables in real learning documents', () => {
  const html = notes.renderNoteMarkdown('## 操作步骤\n\n2. 定义变量\n3. 输出结果\n\n> 注意变量类型\n\n| 类型 | 示例 |\n| --- | --- |\n| `int` | **42** |\n\n---');
  assert.match(html, /<ol start="2">/); assert.match(html, /<blockquote>/);
  assert.match(html, /<table>/); assert.match(html, /<th>类型<\/th>/);
  assert.match(html, /<td><code>int<\/code><\/td>/); assert.match(html, /<hr/);
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

test('legacy note projection and source names render safely without exposing machine labels', () => {
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter(), chapter: { id: 'c', space_id: 's', blocks: [{ id: 'b', kind: 'assessment', markdown: 'opaque-question: incorrect', display_markdown: '## 测评结果\n- 第 1 题：类型转换 — 错误；得分 0', source_refs: [{ material_id: 'm', label: 'Python <img onerror=bad> · 第 3—5 行' }] }] } });
  assert.match(html, /第 1 题：类型转换/);
  assert.match(html, /错误；得分 0/);
  assert.match(html, /查看来源：Python &lt;img onerror=bad&gt; · 第 3—5 行/);
  assert.doesNotMatch(html, /opaque-question|incorrect|<img/);
  assert.match(html, /data-block-index="0" data-source-index="0"/);
});

test('generation draft uses summaries and Chinese verdicts with no hidden answer material', () => {
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter(), chapter: { id: 'c', generations: [{ status: 'failed', snapshot: { topic_ids: ['opaque-topic'], topic_names: ['类型转换'], question_summaries: [{ id: 'opaque-question', summary: '如何处理无效输入？' }], assessment_result: { question_results: [{ question_id: 'opaque-question', verdict: 'incorrect', score: 0, assisted: true }] } } }] } });
  assert.match(html, /类型转换/); assert.match(html, /第 1 题：如何处理无效输入？ · 错误 · 得分 0 · 有辅助记录/);
  assert.doesNotMatch(html, /opaque-topic|opaque-question|incorrect/);
});

test('editing an old note starts from the readable projection and preserves personal blocks', async () => {
  const controller = notes.createNotesController({ api: { notebook: async () => notebookWithGeneratedChapter(), noteChapter: async () => ({ id: 'c', space_id: 's', blocks: [{ id: 'b', markdown: 'opaque', display_markdown: '第 1 题：正确' }, { id: 'p', kind: 'personal', markdown: '我的内容' }] }), noteRevisions: async () => [] } });
  await controller.load({ name: 'space', id: 's', query: { chapter: 'c' } });
  controller.beginEdit(); assert.equal(controller.snapshot().draft[0].markdown, '第 1 题：正确'); assert.equal(controller.snapshot().draft[1].markdown, '我的内容'); controller.dispose();
});

test('generated source labels appear once as controls while handwritten source text remains', () => {
  const chapter = { id: 'c', space_id: 's', blocks: [{ id: 'b', kind: 'assessment', markdown: '## 来源\n- 文档 · 第 1 行\n- 文档 · 第 2 行\n', source_refs: [{ label: '文档 · 第 1 行' }, { label: '文档 · 第 2 行' }] }] };
  const html = notes.renderNotes({ notebook: notebookWithGeneratedChapter(), chapter });
  assert.equal((html.match(/文档 · 第 1 行/g) || []).length, 1);
  assert.equal((html.match(/文档 · 第 2 行/g) || []).length, 1);
  chapter.blocks[0].markdown += '\n我的原文核对备注';
  assert.match(notes.renderNotes({ notebook: notebookWithGeneratedChapter(), chapter }), /我的原文核对备注/);
});
