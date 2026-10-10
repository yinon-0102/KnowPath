import test from 'node:test';
import assert from 'node:assert/strict';
import { esc, validateFile, materialIds, storageRead, storageWrite } from '../src/store.js';

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
test('malformed stored data resets safely and storage failures are surfaced', () => {
  const storage = { getItem: () => '{broken', setItem: () => { throw new Error('Quota'); } };
  assert.deepEqual(storageRead(storage, 'assessment', {}), {});
  storage.getItem = () => JSON.stringify({ assessmentId: 'a1' });
  assert.deepEqual(storageRead(storage, 'assessment'), { assessmentId: 'a1' });
  assert.equal(storageWrite(storage, 'test', {}), false);
});

test('material ids use the backend space bindings', () => {
  assert.deepEqual(materialIds({ bindings: [{ material_id: 'm1' }, { material_id: 'm2' }] }), ['m1', 'm2']);
  assert.deepEqual(materialIds({ bindings: [] }), []);
});
