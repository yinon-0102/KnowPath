import test from 'node:test';
import assert from 'node:assert/strict';
import { currentSpaceMaterialIds, homePlanState, notebookSummary, sortHomeMaterials } from '../src/home-view.js';

test('home context limits materials to the selected space and sorts newest first', () => {
  const space = { id: 's1', bindings: [{ material_id: 'm2' }, { material_id: 'm1' }] };
  const materials = [
    { id: 'm1', name: '旧资料', created_at: '2026-10-01T00:00:00Z' },
    { id: 'm2', name: '新资料', created_at: '2026-10-02T00:00:00Z' },
    { id: 'm3', name: '其他空间', created_at: '2026-10-03T00:00:00Z' },
  ];
  assert.deepEqual(currentSpaceMaterialIds(space), ['m2', 'm1']);
  assert.deepEqual(sortHomeMaterials(materials, space).map(item => item.id), ['m2', 'm1']);
});

test('home plan state distinguishes a plan requiring confirmation from a runnable plan', () => {
  assert.deepEqual(homePlanState({ status: 'needs_replan' }), { action: '查看学习路线', headline: '确认路线，再开始学习。' });
  assert.deepEqual(homePlanState({ status: 'ready' }), { action: '开始学习', headline: '开始下一项学习。' });
  assert.deepEqual(homePlanState(null), { action: '生成学习计划', headline: '先安排一条学习路线。' });
});

test('notebook summary exposes a real empty state and latest generated chapter', () => {
  assert.deepEqual(notebookSummary({ chapters: [] }), { kind: 'empty', label: '尚未生成学习笔记' });
  const summary = notebookSummary({ chapters: [
    { title: '1.1 变量', generation_status: '未总结', updated_at: '2026-10-03' },
    { title: '1.2 循环', generation_status: '已生成', updated_at: '2026-10-04', correction_count: 2 },
  ] });
  assert.equal(summary.kind, 'latest');
  assert.equal(summary.title, '1.2 循环');
  assert.equal(summary.correctionCount, 2);
});
