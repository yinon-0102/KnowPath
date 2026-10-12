import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const source = app.slice(app.indexOf('async function uploadFiles(files)'), app.indexOf('\nasync function viewMaterial'));

test('deduplicated uploads replace an existing card while retaining unrelated materials', async () => {
  const state = { live: { materials: [{ id: 'same', name: 'old' }, { id: 'other' }] } };
  const noop = () => {};
  const fn = new Function('state', 'validateFile', 'openModal', 'api', 'scheduleMaterialRefresh', '$', 'closeModal', 'toast', 'go', source + ';return uploadFiles;')(
    state, noop, noop, { upload: async () => ({ material: { id: 'same', name: 'latest' } }) }, noop, () => null, noop, noop, noop);
  await fn([{ name: 'existing.md' }, { name: 'existing.md' }]);
  assert.deepEqual(state.live.materials, [{ id: 'same', name: 'latest' }, { id: 'other' }]);
});
