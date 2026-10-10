import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { mkdtemp, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createFrontendServer } from '../server.mjs';

function requestStatus(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { headers }, response => {
      response.resume(); resolve(response.statusCode);
    });
    request.on('error', reject);
  });
}

async function authFixture(t, files = {}) {
  const projectRoot = await mkdtemp(path.join(tmpdir(), 'knowpath-auth-'));
  const backend = path.join(projectRoot, 'py');
  await mkdir(backend, { recursive: true });
  for (const [file, content] of Object.entries(files)) await writeFile(path.join(backend, file), content);
  const server = createFrontendServer({ projectRoot });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { backend, base: `http://127.0.0.1:${server.address().port}` };
}

test('static server serves only public assets and rejects foreign hosts', async t => {
  const server = createFrontendServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const port = server.address().port, base = `http://127.0.0.1:${port}`;
  for (const path of ['/', '/src/app.js', '/src/learning.js', '/src/progress.js', '/src/workbench.js', '/src/styles.css', '/assets/knowledge-orbit.svg']) {
    const response = await fetch(base + path); assert.equal(response.status, 200, path);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    assert.equal(response.headers.get('x-knowpath-frontend'), 'knowpath-frontend-v1');
  }
  for (const path of ['/server.mjs', '/README.md', '/.env', '/.git/config', '/py/.learning-token.local', '/%2e%2e/README.md']) assert.equal((await fetch(base + path)).status, 404, path);
  assert.equal((await fetch(base, { method: 'POST' })).status, 405);
  const foreign = await requestStatus(base, { Host: 'unexpected.example' });
  assert.equal(foreign, 403);
});

test('every module imported by the app is served as JavaScript', async t => {
  const server = createFrontendServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
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

test('local auth endpoint reads project configuration only for loopback requests', async t => {
  const { base } = await authFixture(t, { '.env': 'LEARNING_LOCAL_TOKEN=fixture-token\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { token: 'fixture-token', api_base: 'http://127.0.0.1:8000/api/v1' });
  assert.equal(await requestStatus(`${base}/__knowpath/local-auth`, { Host: 'unexpected.example' }), 403);
  assert.equal((await fetch(`${base}/py/.env`)).status, 404);
  assert.equal((await fetch(`${base}/py/.learning-token.local`)).status, 404);
});

test('local auth understands quoted dotenv values with comments and prefers a direct token', async t => {
  const { base } = await authFixture(t, {
    '.env': '\uFEFF# local settings\r\nexport LEARNING_LOCAL_TOKEN = "fixture-token" # comment\r\nLEARNING_LOCAL_TOKEN_FILE=missing.local\r\n',
    '.learning-token.local': 'fallback-token',
  });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'fixture-token');
});

test('local auth decodes single-quoted dotenv backslashes and escaped quotes only', async t => {
  const { base } = await authFixture(t, { '.env': String.raw`LEARNING_LOCAL_TOKEN='fixture\\token\'value\n'` + '\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, "fixture\\token'value\\n");
});

test('local auth decodes double-quoted dotenv backslashes and both quoted escape forms', async t => {
  const { base } = await authFixture(t, { '.env': String.raw`LEARNING_LOCAL_TOKEN="fixture\\token\"value\'suffix\q"` + '\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'fixture\\token"value\'suffix\\q');
});

test('local auth rejects controls and whitespace produced by double-quoted dotenv escape decoding', async t => {
  for (const escape of ['a', 'b', 'f', 'n', 'r', 't', 'v']) {
    const { base } = await authFixture(t, { '.env': `LEARNING_LOCAL_TOKEN="fixture\\${escape}token"\n` });
    const response = await fetch(`${base}/__knowpath/local-auth`);
    assert.equal(response.status, 503, escape);
    assert.deepEqual(await response.json(), { error: '本地服务配置不可用' });
  }
});

test('local auth rejects leading or trailing whitespace in a direct dotenv token', async t => {
  for (const value of ['" fixture-token"', '"fixture-token "', String.raw`"fixture-token\n"`]) {
    const { base } = await authFixture(t, { '.env': `LEARNING_LOCAL_TOKEN=${value}\n` });
    const response = await fetch(`${base}/__knowpath/local-auth`);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: '本地服务配置不可用' });
  }
});

test('local auth expands dotenv references using previously declared values', async t => {
  const { base } = await authFixture(t, { '.env': 'KNOWPATH_AUTH_TEST_PREFIX=fixture\nKNOWPATH_AUTH_TEST_COMBINED=${KNOWPATH_AUTH_TEST_PREFIX}-token\nLEARNING_LOCAL_TOKEN="${KNOWPATH_AUTH_TEST_COMBINED}"\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'fixture-token');
});

test('local auth dotenv references preserve process environment precedence', async t => {
  const name = 'KNOWPATH_AUTH_TEST_PROCESS_PART', original = process.env[name];
  process.env[name] = 'process-fixture';
  t.after(() => { if (original === undefined) delete process.env[name]; else process.env[name] = original; });
  const { base } = await authFixture(t, { '.env': `${name}=dotenv-fixture\nLEARNING_LOCAL_TOKEN='\${${name}}-token'\n` });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'process-fixture-token');
  assert.equal(process.env[name], 'process-fixture');
});

test('local auth dotenv defaults distinguish missing references from defined empty values', async t => {
  const { base } = await authFixture(t, { '.env': 'KNOWPATH_AUTH_TEST_EMPTY=\nLEARNING_LOCAL_TOKEN=${KNOWPATH_AUTH_TEST_MISSING:-fixture}-${KNOWPATH_AUTH_TEST_EMPTY:-unused}${KNOWPATH_AUTH_TEST_LATER}\nKNOWPATH_AUTH_TEST_LATER=later\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'fixture-');
});

test('local auth expands dotenv references in a configured token file path', async t => {
  const { base } = await authFixture(t, { '.env': 'KNOWPATH_AUTH_TEST_FILE=interpolated.local\nLEARNING_LOCAL_TOKEN_FILE=${KNOWPATH_AUTH_TEST_FILE}\n', 'interpolated.local': 'file-fixture-token' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'file-fixture-token');
});

test('local auth preserves explicitly empty process auth settings over dotenv defaults', async t => {
  const names = ['LEARNING_LOCAL_TOKEN', 'LEARNING_LOCAL_TOKEN_FILE'];
  const originals = names.map(name => process.env[name]);
  for (const name of names) process.env[name] = '';
  t.after(() => names.forEach((name, index) => {
    if (originals[index] === undefined) delete process.env[name]; else process.env[name] = originals[index];
  }));
  const { base } = await authFixture(t, {
    '.env': 'LEARNING_LOCAL_TOKEN=dotenv-token\nLEARNING_LOCAL_TOKEN_FILE=dotenv-file.local\n',
    '.learning-token.local': 'default-file-token', 'dotenv-file.local': 'dotenv-file-token',
  });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'default-file-token');
});

test('local auth uses the default token file and rereads rotations without caching', async t => {
  const { backend, base } = await authFixture(t, { '.learning-token.local': '  first-fixture-token\r\n' });
  assert.equal((await (await fetch(`${base}/__knowpath/local-auth`)).json()).token, 'first-fixture-token');
  await writeFile(path.join(backend, '.learning-token.local'), 'second-fixture-token\n');
  assert.equal((await (await fetch(`${base}/__knowpath/local-auth`)).json()).token, 'second-fixture-token');
});

test('local auth resolves a configured token file relative to the backend directory', async t => {
  const { base } = await authFixture(t, { '.env': "LEARNING_LOCAL_TOKEN_FILE='custom.local'\n", 'custom.local': 'custom-fixture-token\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'custom-fixture-token');
});

test('local auth accepts an absolute token file path from dotenv', async t => {
  const { backend, base } = await authFixture(t, { 'absolute.local': 'absolute-fixture-token' });
  const escapedPath = path.join(backend, 'absolute.local').replace(/\\/g, '\\\\');
  await writeFile(path.join(backend, '.env'), `LEARNING_LOCAL_TOKEN_FILE="${escapedPath}"\n`);
  const response = await fetch(`${base}/__knowpath/local-auth`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).token, 'absolute-fixture-token');
});

test('local auth decodes escaped backslashes in a quoted configured file path', async t => {
  const { backend, base } = await authFixture(t);
  const tokenDirectory = path.join(backend, 'tokens');
  await mkdir(tokenDirectory, { recursive: true });
  const tokenFile = path.join(tokenDirectory, 'auth.local');
  await writeFile(tokenFile, 'path-fixture-token');
  for (const quote of ["'", '"']) {
    const escapedPath = tokenFile.replace(/\\/g, '\\\\');
    await writeFile(path.join(backend, '.env'), `LEARNING_LOCAL_TOKEN_FILE=${quote}${escapedPath}${quote}\n`);
    const response = await fetch(`${base}/__knowpath/local-auth`);
    assert.equal(response.status, 200, quote);
    assert.equal((await response.json()).token, 'path-fixture-token');
  }
});

test('local auth supports HEAD and rejects writes', async t => {
  const { base } = await authFixture(t, { '.env': 'LEARNING_LOCAL_TOKEN=fixture-token\n' });
  const response = await fetch(`${base}/__knowpath/local-auth`, { method: 'HEAD' });
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /^application\/json/);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('access-control-allow-origin'), null);
  assert.equal(await response.text(), '');
  assert.equal((await fetch(`${base}/__knowpath/local-auth`, { method: 'POST' })).status, 405);
});

test('local auth reports unavailable and invalid tokens without configuration details', async t => {
  for (const files of [{}, { '.learning-token.local': '  \n' }, { '.env': 'LEARNING_LOCAL_TOKEN="invalid token"\n' }, { '.env': 'LEARNING_LOCAL_TOKEN="unterminated-token\n' }]) {
    const { base } = await authFixture(t, files);
    const response = await fetch(`${base}/__knowpath/local-auth`);
    assert.equal(response.status, 503);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await response.json(), { error: '本地服务配置不可用' });
  }
});
