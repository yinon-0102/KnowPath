import test from 'node:test';
import assert from 'node:assert/strict';
import { esc, validateFile, makeDemo, loadDemo, spaceProgress, materialIds, storageWrite } from '../src/store.js';

test('user-controlled text is escaped before HTML rendering', () => {
  assert.equal(esc('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(esc('" & \''), '&quot; &amp; &#39;');
});
test('uploads reject unsupported formats, empty files and oversized content', () => {
  assert.throws(() => validateFile({ name: 'data.exe', size: 30 }));
  assert.throws(() => validateFile({ name: 'data.md', size: 0 }));
  assert.throws(() => validateFile({ name: 'data.pdf', size: 20 * 1024 * 1024 + 1 }));
  assert.doesNotThrow(() => validateFile({ name: '学习笔记.MD', size: 100 }));
});
test('demo graph, task and material references all resolve', () => {
  const demo = makeDemo(new Date('2026-09-26T00:00:00Z'));
  const materials = new Set(demo.materials.map(m => m.id));
  const spaces = new Set(demo.spaces.map(s => s.id));
  for (const space of demo.spaces) {
    assert.ok(materialIds(space).every(id => materials.has(id)));
    const nodes = new Set(space.nodes.map(n => n.id));
    assert.ok(space.edges.every(e => nodes.has(e.from_id) && nodes.has(e.to_id)));
    assert.ok(space.topic_ids.every(id => nodes.has(id)));
  }
  assert.ok(demo.tasks.every(t => spaces.has(t.space_id)));
  assert.equal(spaceProgress(demo.spaces[0]), 38);
});
test('malformed stored data resets safely and storage failures are surfaced', () => {
  const storage = { getItem: () => '{broken', setItem: () => { throw new Error('Quota'); } };
  assert.equal(loadDemo(storage).spaces.length, 3);
  storage.getItem = () => JSON.stringify({ version: 1, spaces: [{}], materials: [], tasks: [] });
  assert.equal(loadDemo(storage).spaces.length, 3);
  assert.equal(storageWrite(storage, 'test', {}), false);
});
