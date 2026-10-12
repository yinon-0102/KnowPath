import test from 'node:test';
import assert from 'node:assert/strict';
import * as progress from '../src/progress.js';

const task = { id: 't', task_id: 't', node_id: 'n', plan_id: 'p', title: '3. 循环、range 与列表', status: 'pending', sequence: 8, context: { position: 0 }, active_session_id: 's' };
const data = { space_id: 'space', rounds: [{ plan_id: 'p', nodes: [task] }] };

test('unprefixed topic retains reliable source locator rather than missing-position hint', () => {
  const node = { ...task, title: '循环', context: { position: 0, source_refs: [{ line_start: 37 }] } };
  assert.equal(progress.taskDisplay(node).sourceLabel, '第 37 行');
  assert.equal(progress.taskDisplay({ ...node, context: { position: 0 } }).sourceLabel, '原文位置未记录');
});
test('route distinguishes display position, source chapter and stable historical numbering', () => {
  const html = progress.renderProgress(data, { currentPlan: { id: 'p', tasks: [task] }, compact: true });
  assert.match(html, /节点 1/);
  assert.match(html, /原文章节 3/);
  assert.doesNotMatch(html, /节点 8/);
  assert.equal(progress.taskDisplay(task, 0).title, '节点 1 · 循环、range 与列表');
  assert.equal(progress.taskDisplay({ ...task, historical: true }, 0).title, '节点 8 · 循环、range 与列表');
});
test('only a verified unpaused session animates, browsing and unknown pause state do not', () => {
  const render = activeSession => progress.renderProgress(data, { compact: true, activeSession });
  assert.match(render({ id: 's', status: 'active', paused: false }), /running/);
  assert.doesNotMatch(render({ id: 'other', status: 'active', paused: false }), /running/);
  assert.doesNotMatch(render({ id: 's', status: 'active' }), /running/);
  assert.match(render({ id: 's', status: 'active', paused: true }), /已暂停/);
  assert.doesNotMatch(render({ id: 's', status: 'active', paused: true }), /running/);
});
test('completed, skipped and deferred nodes do not animate with old active references', () => {
  for (const status of ['completed', 'skipped', 'deferred']) {
    const html = progress.renderProgress({ ...data, rounds: [{ plan_id: 'p', nodes: [{ ...task, status }] }] }, { compact: true, activeSession: { id: 's', status: 'active', paused: false } });
    assert.doesNotMatch(html, /running/);
  }
});
test('chapter metadata wins over guessed chapter prefixes and hostile values are escaped', () => {
  assert.equal(progress.taskDisplay({ ...task, context: { source_label: '原文章节 第三章' } }, 0).sourceLabel, '原文章节 第三章');
  const html = progress.renderProgress({ ...data, rounds: [{ plan_id: 'p', nodes: [{ ...task, title: '<img onerror=bad>' }] }] }, { compact: true });
  assert.doesNotMatch(html, /<img/);
});
