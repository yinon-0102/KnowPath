import test from 'node:test';
import assert from 'node:assert/strict';
import * as picker from '../src/scope-picker.js';
import { renderWorkspace } from '../src/workspace-view.js';
const topics = [{ id: 'chapter', name: 'Chapter', is_learnable: false, content_role: 'container', source_refs: [{ material_id: 'm1' }] },
  ...Array.from({ length: 126 }, (_, i) => ({ id: `t${i}`, name: `Unit ${i}`, parent_id: 'chapter', is_learnable: true, source_refs: [{ material_id: 'm1' }] })),
  { id: 'alias', canonical_topic_id: 't1', name: 'Unit 1', is_learnable: true }];
test('large chapter batch includes effective descendants once and filtered batch preserves hidden selections', () => {
  const model = picker.scopeModel(topics, { topic_ids: ['t125'], excluded_topic_ids: ['t1'] });
  assert.equal(model.units.length, 126);
  picker.scopeBatch(model, { parent: 'chapter', selected: true, query: 'Unit 1' });
  assert.ok(model.selected.has('t125'));
  assert.ok(model.selected.has('t1'));
  assert.ok(!model.excluded.has('t1'));
  assert.ok(!model.selected.has('chapter'));
  picker.scopeBatch(model, { parent: 'chapter', selected: true });
  assert.equal(model.selected.size, 126);
});
test('both forms render persistent prerequisites, explicit mode, result-only batch controls, counts and source reasons', () => {
  const html = picker.renderScopePicker(topics, { topic_ids: ['t0'], include_prerequisites: false, scope_mode: 'custom', prerequisite_additions: [{ topic_id: 't0', required_by_topic_id: 't1', reason: '必要前置知识', source_refs: [{ material_id: 'm1', page: 2 }] }] }, [{ id: 'm1', name: 'Textbook' }]);
  assert.match(html, /学习全部有效内容/);
  assert.match(html, /自选章节/);
  assert.match(html, /仅搜索结果/);
  assert.match(html, /预计/);
  assert.match(html, /第 2 页/);
  assert.doesNotMatch(html, /name="prerequisites"[^>]*checked/);
  assert.doesNotMatch(html, /name="topic_ids" value="chapter"/);
});
test('unknown scope write replays original body and key across changed selection and reload', async () => {
  const map = new Map(), storage = { getItem: k => map.get(k), setItem: (k,v) => map.set(k,v), removeItem: k => map.delete(k) };
  const calls = []; let fail = true;
  const api = { setScope: async (...args) => { calls.push(args); if (fail) throw Object.assign(new Error('timeout'), { status: 0 }); return { space_version: 3, topic_ids: args[1] }; } };
  const save = picker.createScopeSaver({ api, storage });
  await assert.rejects(save({ id: 's', space_version: 2 }, { topic_ids: ['t0'], excluded_topic_ids: [], include_prerequisites: false, scope_mode: 'custom' }));
  fail = false;
  const retry = picker.createScopeSaver({ api, storage });
  const result = await retry({ id: 's', space_version: 9 }, { topic_ids: ['t2'], excluded_topic_ids: [], include_prerequisites: true, scope_mode: 'all' });
  assert.deepEqual(calls[1], calls[0]);
  assert.deepEqual(result.topic_ids, ['t0']);
});
test('management entry uses the shared chapter picker and saved prerequisite flag', () => {
  const html = renderWorkspace({ route: { page: 'space-scope', id: 's' }, data: { space: { id: 's', topic_ids: ['t0'], include_prerequisites: false }, topics, materials: [] }, filters: {} });
  assert.match(html, /data-scope-picker/);
  assert.match(html, /data-scope-parent="chapter"/);
  assert.doesNotMatch(html, /name="prerequisites"[^>]*checked/);
});
test('only real chapters with learnable descendants offer chapter batch and workload states its basis', () => {
  const html = picker.renderScopePicker([...topics, { id: 'footer', name: 'Footer', is_learnable: false, content_role: 'reference' }], { topic_ids: ['t0'] }, [{ id: 'm1', name: 'Textbook' }]);
  assert.doesNotMatch(html, /data-scope-parent="footer"/);
  assert.match(html, /全选整份资料/);
  assert.match(html, /每单元约 10 分钟/);
  assert.match(html, /不含测评/);
});
test('chapter containing only subchapters still batches effective grandchildren without an introduction', () => {
  const nested = [{ id: 'root', name: 'Root', is_learnable: false, content_role: 'container' }, { id: 'sub', name: 'Sub', parent_id: 'root', is_learnable: false, content_role: 'container' }, { id: 'leaf', name: 'Leaf', parent_id: 'sub', is_learnable: true }];
  assert.match(picker.renderScopePicker(nested, { topic_ids: [] }), /data-scope-parent="root"/);
  const model = picker.scopeModel(nested, {});
  picker.scopeBatch(model, { parent: 'root' });
  assert.deepEqual([...model.selected], ['leaf']);
});
test('storage quota cannot replace an unknown scope identity and retry ignores invalid later edits', async () => {
  const calls = []; let fail = true;
  const storage = { getItem: () => null, setItem: () => { throw new Error('quota'); } };
  const api = { setScope: async (...args) => { calls.push(args); if (fail) throw Object.assign(new Error('timeout'), { status: 0 }); return { space_version: 2 }; } };
  const save = picker.createScopeSaver({ api, storage });
  await assert.rejects(save({ id: 's', space_version: 1 }, { topic_ids: ['t0'], excluded_topic_ids: [], include_prerequisites: false, scope_mode: 'custom' }));
  fail = false;
  await save({ id: 's', space_version: 9 }, () => { throw new Error('later empty draft'); });
  assert.deepEqual(calls[1], calls[0]);
});
test('a late response in one space preserves another space unknown request journal', async () => {
  const map = new Map(), storage = { getItem: k => map.get(k), setItem: (k,v) => map.set(k,v) };
  const body = { topic_ids: ['t0'], excluded_topic_ids: [], include_prerequisites: false, scope_mode: 'custom' };
  let finishA, failB; const calls = [];
  const api = { setScope: (...args) => { calls.push(args); return new Promise((resolve, reject) => { if (args[0].id === 'a') finishA = resolve; else failB = reject; }); } };
  const saveA = picker.createScopeSaver({ api, storage }), saveB = picker.createScopeSaver({ api, storage });
  const a = saveA({ id: 'a', space_version: 1 }, body), b = saveB({ id: 'b', space_version: 2 }, body);
  const rejected = assert.rejects(b);
  finishA({ space_version: 2 }); await a;
  failB(Object.assign(new Error('timeout'), { status: 0 })); await rejected;
  const retry = picker.createScopeSaver({ storage, api: { setScope: async (...args) => { calls.push(args); return { space_version: 3 }; } } });
  await retry({ id: 'b', space_version: 10 }, { ...body, topic_ids: ['t2'] });
  assert.deepEqual(calls[2], calls[1]);
});
