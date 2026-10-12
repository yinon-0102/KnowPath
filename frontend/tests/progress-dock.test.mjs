import test from 'node:test';
import assert from 'node:assert/strict';
import * as progress from '../src/progress.js';

function fixture() {
  let callback, observed, disconnected = 0;
  const changes = [], dock = { inert: false, classList: { toggle: (name, value) => changes.push([name, value]) }, setAttribute(name, value) { this[name] = value; } };
  const panel = { getBoundingClientRect: () => ({ bottom: 300 }) };
  class Observer { constructor(fn, options) { callback = fn; this.options = options; } observe(value) { observed = value; } disconnect() { disconnected++; } }
  assert.equal(typeof progress.createProgressDock, 'function');
  const controller = progress.createProgressDock({ Observer, offset: 80 });
  controller.mount(panel, dock);
  return { controller, panel, dock, changes, callback: (...args) => callback(...args), observed: () => observed, disconnected: () => disconnected };
}

test('route dock observes a stable full route and does not mutate its geometry', () => {
  const f = fixture(); assert.equal(f.observed(), f.panel); assert.equal(f.dock.inert, true);
  f.callback([{ target: f.panel, boundingClientRect: { bottom: 75 } }]); assert.equal(f.dock.inert, false); assert.equal(f.dock['aria-hidden'], 'false');
  f.callback([{ target: f.panel, boundingClientRect: { bottom: 90 } }]); assert.equal(f.dock.inert, true); assert.equal(f.panel.style, undefined);
});

test('same geometry observation never repeatedly changes state and old observations are ignored', () => {
  const f = fixture(); f.callback([{ target: f.panel, boundingClientRect: { bottom: 70 } }]); const count = f.changes.length;
  f.callback([{ target: f.panel, boundingClientRect: { bottom: 70 } }]); assert.equal(f.changes.length, count);
  f.controller.dispose(); f.callback([{ target: f.panel, boundingClientRect: { bottom: 90 } }]); assert.equal(f.changes.length, count); assert.ok(f.disconnected());
});
