import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { createFrontendServer } from '../server.mjs';

test('static server serves only public assets and rejects foreign hosts', async t => {
  const server = createFrontendServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const port = server.address().port, base = `http://127.0.0.1:${port}`;
  for (const path of ['/', '/src/app.js', '/src/styles.css', '/assets/knowledge-orbit.svg']) {
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
