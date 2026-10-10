import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
function startup(loadModule) {
  const button = { addEventListener(_, callback) { this.click = callback; } };
  const main = { innerHTML: '正在打开你的学习空间…', querySelector: () => button };
  let reloads = 0;
  const context = vm.createContext({ loadModule, document: { getElementById: () => main }, location: { reload: () => reloads++ } });
  const start = app.indexOf('async function loadFrontendModules(');
  assert.ok(start >= 0, 'startup must handle dependency load failures');
  const source = app.slice(start, app.indexOf('\nconst [', start));
  vm.runInContext(source.replaceAll('import(', 'loadModule('), context);
  return { load: context.loadFrontendModules, main, button, reloads: () => reloads };
}

test('missing study module replaces initial loading with recovery and reload action', async () => {
  const env = startup(async name => { if (name === './study.js') throw new TypeError('Failed to fetch dynamically imported module'); return {}; });
  await assert.rejects(env.load(), /Failed to fetch/);
  assert.match(env.main.innerHTML, /重启前端服务/);
  assert.doesNotMatch(env.main.innerHTML, /正在打开/);
  env.button.click(); assert.equal(env.reloads(), 1);
});

test('successful module loading leaves the page for normal initialization', async () => {
  const imports = [], env = startup(async name => { imports.push(name); return { name }; });
  const modules = await env.load();
  assert.equal(modules.length, 12); assert.ok(imports.includes('./study.js')); assert.ok(imports.includes('./plan-order.js')); assert.ok(imports.includes('./home-view.js'));
  assert.match(env.main.innerHTML, /正在打开/);
});
