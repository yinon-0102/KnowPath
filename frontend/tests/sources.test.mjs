import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../src/api.js';
import { createPdfLibrary, pdfKey, validatePdf, sourcePage, pdfLocation } from '../src/store.js';

const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
const ref = { material_id: 'm/1', material_version_id: 'old-version', chunk_id: 'chunk 3' };

test('graph source consults its exact version with space context and local authentication', async t => {
  let captured;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    captured = { url: new URL(url), options };
    if (!captured.url.searchParams.get('space_id')) return json({ error: { code: 'SPACE_CONTEXT_REQUIRED' } }, 409);
    return json({ ...ref, text: '第七页原文', page: 7 });
  });
  const source = await createApi(() => 'private-token').source(ref, 'space/one');
  assert.equal(captured.url.pathname, '/api/v1/materials/m%2F1/versions/old-version/chunks/chunk%203');
  assert.equal(captured.url.searchParams.get('space_id'), 'space/one');
  assert.equal(captured.options.headers['X-Local-Token'], 'private-token');
  assert.ok(!captured.url.href.includes('private-token'));
  assert.equal(pdfLocation('blob:test', source), 'blob:test#page=7&view=FitH');
});

test('new citations resolve original spans rather than querying a retrieval chunk as an original', async t => {
  const citation = { ...ref, citation_schema_version: 2, retrieval_version_id: 'retrieval-v2', source_spans: [{ material_version_id: 'old-version', block: 'original-chunk', start: 0, end: 20, page: 9 }] };
  let captured;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    captured = { url, options };
    return json({ ...citation, text: '已核对的原文' });
  });
  const result = await createApi(() => '').source({ ...citation, label: 'not-part-of-the-contract' }, 's1');
  assert.ok(captured.url.endsWith('/learning-spaces/s1/citations/resolve'));
  assert.equal(captured.options.method, 'POST');
  assert.deepEqual(JSON.parse(captured.options.body), citation);
  assert.equal(sourcePage(result), 9);
});

test('unavailable or out-of-scope sources remain errors, not unaudited cached reads', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => { calls.push(url); return json({ error: { code: 'SOURCE_OUT_OF_SCOPE', message: '来源不属于该空间' } }, 409); });
  const api = createApi(() => '');
  assert.throws(() => api.source(ref), error => error.code === 'SPACE_CONTEXT_REQUIRED');
  assert.throws(() => api.source({ material_id: 'm' }, 's'), error => error.code === 'INVALID_SOURCE');
  assert.throws(() => api.source({ ...ref, citation_schema_version: 8 }, 's'), error => error.code === 'INVALID_SOURCE');
  assert.equal(calls.length, 0);
  await assert.rejects(() => api.source(ref, 's'), error => error.code === 'SOURCE_OUT_OF_SCOPE');
  assert.equal(calls.length, 1);
});

test('whole-material reading also carries its selected space', async t => {
  let url;
  t.mock.method(globalThis, 'fetch', async input => { url = new URL(input); return json({ material: {} }); });
  await createApi(() => '').material('material1', 'space1');
  assert.equal(url.searchParams.get('space_id'), 'space1');
});

test('page navigation only trusts positive original PDF page coordinates', () => {
  for (const value of [undefined, null, 0, -2, '7', 2.5]) assert.equal(sourcePage({ page: value }), null);
  assert.equal(sourcePage({ line_start: 37, section_path: ['第37条'] }), null);
  assert.equal(pdfLocation('blob:pdf', { page: 7 }, false), 'blob:pdf#view=FitH');
  assert.equal(pdfLocation('blob:pdf', { source_spans: [{ page: 4 }, { page: 5 }] }), 'blob:pdf#page=4&view=FitH');
});

test('original PDF associations verify exact file bytes, including historical versions', async () => {
  const file = new File(['%PDF-1.7\noriginal\n%%EOF'], 'original.PDF', { type: 'application/pdf' });
  const hash = await validatePdf(file);
  assert.match(hash, /^[a-f0-9]{64}$/);
  assert.equal(await validatePdf(file, hash), hash);
  await assert.rejects(() => validatePdf(new File(['%PDF-1.7\ndifferent\n%%EOF'], 'changed.pdf'), hash), /版本不一致/);
  await assert.rejects(() => validatePdf(new File(['not PDF'], 'fake.pdf')), /不是有效/);
  await assert.rejects(() => validatePdf(new File([], 'empty.pdf')), /大于 0/);
  await assert.rejects(() => validatePdf(new File(['%PDF'], 'file.txt')), /请选择 PDF/);
});

test('local PDF lookup is separated by mode, material and version; storage failure retains a session copy', async () => {
  const library = createPdfLibrary(null);
  const key = pdfKey('live', 'material1', 'version1');
  const file = new File(['%PDF-1.7\noriginal'], 'original.pdf');
  assert.equal(await library.save({ key, file, hash: await validatePdf(file), verified: true }), false);
  assert.equal((await library.get(key)).blob.type, 'application/pdf');
  assert.equal((await library.get(key)).name, 'original.pdf');
  assert.equal(library.peek(key).verified, true);
  for (const other of [pdfKey('demo', 'material1', 'version1'), pdfKey('live', 'material2', 'version1'), pdfKey('live', 'material1', 'version2')]) assert.equal(await library.get(other), undefined);
});
