import test from 'node:test';
import assert from 'node:assert/strict';
import { buildGraphIndex, graphSlice, graphTitle, graphRelation } from '../src/store.js';

test('large supplied document hierarchies remain fully reachable without dense views or a 100-node cutoff', () => {
  const nodes = [{ id: 'root', name: '法律原文（包含很长的公布与修订说明）' }, ...Array.from({ length: 503 }, (_, i) => ({ id: 'n' + i, name: '第' + (i + 1) + '条 完整的学习内容', parent_id: 'root' }))];
  const index = buildGraphIndex({ nodes, edges: [] });
  assert.equal(index.roots.length, 1);
  assert.equal(index.edges.length, 503);
  const seen = new Set();
  const first = graphSlice(index, { focusId: 'root' });
  assert.equal(first.total, 503);
  for (let page = 0; page < first.pages; page++) {
    const slice = graphSlice(index, { focusId: 'root', page });
    assert.ok(slice.items.length <= 4);
    slice.items.forEach(entry => { assert.ok(!seen.has(entry.node.id)); seen.add(entry.node.id); });
  }
  assert.equal(seen.size, 503);
  const result = graphSlice(index, { query: '第503条' });
  assert.equal(result.items[0].node.id, 'n502');
});

test('search is global, includes complete titles and descriptions, and overrides current branch', () => {
  const index = buildGraphIndex({ nodes: [{ id: 'r', name: '总则' }, { id: 'a', name: '第一条 学校保护', parent_id: 'r' }, { id: 'b', title: 'Language Study', description: 'special evidence outside branch' }], edges: [] });
  assert.equal(graphSlice(index, { focusId: 'a', query: 'EVIDENCE', relation: 'children' }).items[0].node.id, 'b');
  assert.equal(graphSlice(index, { focusId: 'a', query: 'language' }).items[0].node.id, 'b');
  assert.equal(graphSlice(index, { query: '不存在' }).total, 0);
  assert.equal(graphTitle(index.nodes[1]), '第一条 学校保护');
});

test('neighbors distinguish containment and prerequisite directions and merge repeated links', () => {
  const nodes = ['parent','focus','child','before','after'].map(id => ({ id, name:id }));
  const edges = [
    { id:'e1', from_id:'parent', to_id:'focus', type:'contains' },
    { id:'e2', from_id:'focus', to_id:'child', type:'contains' },
    { id:'e3', from_id:'before', to_id:'focus', type:'prerequisite_of' },
    { id:'e4', from_id:'focus', to_id:'after', type:'prerequisite_of' },
    { id:'e5', from_id:'before', to_id:'focus', type:'related_to' },
    { id:'repeat', from_id:'parent', to_id:'focus', type:'contains' },
  ];
  const index = buildGraphIndex({ nodes, edges });
  assert.equal(index.adjacent.get('focus').size, 4);
  assert.deepEqual(graphSlice(index,{focusId:'focus',relation:'children'}).items.map(e=>e.node.id),['child']);
  assert.deepEqual(graphSlice(index,{focusId:'focus',relation:'parents'}).items[0].labels,['上级主题']);
  assert.deepEqual(graphSlice(index,{focusId:'focus',relation:'prerequisites'}).items[0].labels,['前置知识']);
  assert.equal(graphRelation(edges[3],'focus'),'后续知识');
});

test('disconnected nodes are browsable without invented connections; rejected parent links stay rejected', () => {
  const index = buildGraphIndex({ nodes: [{ id:'a', name:'第一条 保护', parent_id:'b' },{ id:'b', name:'第一条 学习' },{ id:'b', name:'duplicate' }], edges: [
    { from_id:'b', to_id:'a', type:'contains', status:'rejected' },
    { from_id:'a', to_id:'missing', type:'related_to' },
    { from_id:'a', to_id:'a', type:'related_to' },
  ] });
  assert.equal(index.nodes.length,2);
  assert.equal(index.edges.length,0);
  assert.equal(graphSlice(index).total,2);
  assert.equal(graphSlice(index,{focusId:'a'}).total,0);
});

test('cycles, disconnected components, unknown IDs and out-of-range pages do not hide nodes or loop', () => {
  const index = buildGraphIndex({ nodes: [{id:'a',name:'a',parent_id:'b'},{id:'b',name:'b',parent_id:'a'},{id:'c',name:'c'}] });
  assert.equal(graphSlice(index,{scope:'all'}).total,3);
  assert.equal(graphSlice(index,{focusId:'a'}).total,1);
  assert.equal(graphSlice(index,{focusId:'unknown',scope:'all',page:999}).page,0);
  assert.equal(graphSlice(index,{page:-100}).page,0);
  assert.equal(graphSlice(buildGraphIndex()).total,0);
  assert.equal(graphSlice(buildGraphIndex()).pages,1);
});

test('relation statuses and title content remain honest and the original graph is never mutated', () => {
  const graph={ nodes:[{id:'a',name:'第一条 <script>example</script>'},{id:'b',name:'title'}],edges:[{from_id:'a',to_id:'b',type:'prerequisite_of',status:'pending'}]};
  const original=structuredClone(graph),index=buildGraphIndex(graph);
  const view=graphSlice(index,{focusId:'a'});
  assert.deepEqual(graph,original);
  assert.deepEqual(view.items[0].labels,['后续知识（待确认）']);
  assert.equal(graphTitle(graph.nodes[0]),original.nodes[0].name);
  assert.equal(graphTitle({name:'  标题  含换行\n文本 '}),'标题 含换行 文本');
});
