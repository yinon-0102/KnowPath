import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { createFrontendServer } from '../server.mjs';
import { access } from 'node:fs/promises';

test('feature modules have static routes and every app dependency is served as JavaScript', async t => {
  const server = createFrontendServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  for (const name of ['notes.js', 'progress.js', 'workbench.js']) {
    let exists = true;
    try { await access(new URL(`../src/${name}`, import.meta.url)); }
    catch (error) { if (error.code !== 'ENOENT') throw error; exists = false; }
    const response = await fetch(`${base}/src/${name}`);
    assert.equal(response.status, exists ? 200 : 503, name);
    if (exists) assert.match(response.headers.get('content-type'), /^text\/javascript/, name);
  }
  const pending = ['/src/app.js'], visited = new Set();
  while (pending.length) {
    const pathname = pending.pop();
    if (visited.has(pathname)) continue;
    visited.add(pathname);
    const response = await fetch(base + pathname);
    assert.equal(response.status, 200, pathname);
    assert.match(response.headers.get('content-type'), /^text\/javascript/, pathname);
    const source = await response.text();
    for (const match of source.matchAll(/\bimport\s+(?:[^'";]*?\s+from\s+)?['"](\.[^'"]+)['"]/g)) {
      pending.push(new URL(match[1], base + pathname).pathname);
    }
  }
});

test('static server serves only public assets and rejects foreign hosts', async t => {
  const server = createFrontendServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const port = server.address().port, base = `http://127.0.0.1:${port}`;
  for (const path of ['/', '/src/app.js', '/src/learning.js', '/src/styles.css', '/assets/knowledge-orbit.svg']) {
    const response = await fetch(base + path); assert.equal(response.status, 200, path);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  }
  for (const path of ['/server.mjs', '/README.md', '/.env', '/.git/config', '/py/.learning-token.local', '/%2e%2e/README.md']) assert.equal((await fetch(base + path)).status, 404, path);
  assert.equal((await fetch(base, { method: 'POST' })).status, 405);
  const foreign = await new Promise((resolve, reject) => {
    const request = http.get(base, { headers: { Host: 'unexpected.example' } }, response => { response.resume(); resolve(response.statusCode); });
    request.on('error', reject);
  });
  assert.equal(foreign, 403);
});
