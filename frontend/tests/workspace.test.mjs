import test from 'node:test';
import assert from 'node:assert/strict';
import { createWorkspaceController, updatedBindings, taskPayload, resolutionPayload, correctionPayload, dateRange } from '../src/workspace.js';
import * as workspaceModule from '../src/workspace.js';
import { renderWorkspace, workspaceTitles, correctionFields, updateWorkspaceConditions } from '../src/workspace-view.js';
import { createApi, ApiError } from '../src/api.js';

const memory = () => { const map = new Map(); return { getItem: key => map.get(key) || null, setItem: (key, value) => map.set(key, value) }; };

test('reparse and retry are distinct commands and failures offer recovery', async () => {
  for (const [page, method] of [['material-reparse', 'reparse'], ['material-retry', 'retry']]) {
    let called;
    const env = environment(page+'/m1', { [method]: async (id, body) => { called = { id, body }; return { run_id: 'new-run', status: 'queued' }; } });
    await env.controller.load();
    await env.controller.submit(values({ version_id: 'v1' }));
    assert.deepEqual(called, { id: 'm1', body: { version_id: 'v1' } });
  }
  const env = environment('material-graph/m1', { graphDiff: async () => ({ ...diff, status: 'failed', error: { message: '索引准备失败', retryable: true, details: { stage: 'preparing' } } }) });
  await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /索引准备失败/);
  assert.match(html, /重试失败任务/);
  assert.doesNotMatch(html, /请稍后刷新/);
  assert.match(html, /role="alert"/);
  assert.match(html, /索引与图谱准备/);
  env.api.graphDiff = async () => ({ ...diff, status: 'failed', error: { message: '原文件缺失', retryable: false, details: { stage: 'parsing', retryable: false } } });
  await env.controller.load({ force: true });
  const terminal = renderWorkspace(env.controller.snapshot());
  assert.doesNotMatch(terminal, />重试失败任务</);
});
const values = object => { const result = new FormData(); for (const [key, value] of Object.entries(object)) for (const item of Array.isArray(value) ? value : [value]) result.append(key, item); return result; };
const ref = { material_id: 'm1', material_version_id: 'v1', chunk_id: 'c1', page: 2 };
const material = { id: 'm1', name: '<img onerror=alert(1)>教材', status: 'ready', version: 3, current_version_id: 'v1' };
const space = { id: 's1', name: '线性代数', space_version: 4, bindings: [{ material_id: 'm1', material_version_id: 'v1', graph_version: 2 }, { material_id: 'm2', material_version_id: 'v2', graph_version: 1 }], topic_ids: ['t1'] };
const topic = { id: 't1', name: '线性变换', description: '保持线性结构', source_refs: [ref] };
const question = { id: 'q1', type: 'single_choice', topic_id: 't1', prompt: '什么是线性变换？', options: [{ id: 'A', text: '保持线性' }, { id: 'B', text: '任意映射' }], source_refs: [ref], answer_key: 'A', rubric: '线性性质' };
const review = { review_id: 'r1', question_id: 'q1', question, frozen_question: question, source_text: '封存的原文', status: 'pending', events: [{ action: 'report', reason: '表述不清楚' }] };
const plan = { id: 'p1', space_id: 's1', status: 'ready', version: 4, tasks: [{ id: 'task1', topic_ids: ['t1'], status: 'pending', estimated_minutes: 25 }] };
const diff = { candidate_revision_id: 'revision1', base_graph_version: 2, status: 'pending_review', added: [], changed: [{ kind: 'node', before: topic, after: { ...topic, name: '新版线性变换' } }], removed: [], conflicts: [] };
function fixtures() {
  return {
    materials: async () => [structuredClone(material)], materialVersions: async () => [{ id: 'v1', filename: '教材.pdf', status: 'ready' }], spaces: async () => [structuredClone(space)],
    space: async () => structuredClone(space), topics: async () => [topic], graph: async () => ({ nodes: [topic], edges: [] }),
    profile: async () => ({ profile_version: 2, profile: { goal: { value: '理解线性变换' }, weekly_minutes: { value: 150 }, preferences: { value: { example_first: true } } } }),
    graphDiff: async () => structuredClone(diff), assessment: async () => ({ id: 'a1', space_id: 's1', status: 'completed', questions: [question], answers: [{ question_id: 'q1', answer: 'B' }] }),
    assessmentResult: async () => ({ assessment_id: 'a1', question_results: [{ question_id: 'q1', score: 0, verdict: 'incorrect', feedback: '<script>反馈</script>', source_refs: [ref] }], topic_results: [{ topic_id: 't1', score: 0, verified_count: 1, unverified_count: 0 }] }),
    questionReviews: async () => ({ assessment_id: 'a1', review_version: 5, items: [review] }),
    knowledgeUpdates: async () => ({ space_id: 's1', bindings: space.bindings, available_updates: [{ material_id: 'm1', material_version_id: 'v3', graph_version: 4, graph_revision_id: 'gr4' }], affected_topic_ids: ['t1'], invalidated_question_ids: ['q1'] }),
    changes: async () => ({ items: [{ kind: 'material_version', created_at: '2026-10-05' }] }), learningState: async () => ({ space_id: 's1', state_version: 3, items: [{ topic_id: 't1', status: 'needs_review', score_validity: 'stale', mastery_score: 1 }] }),
    evidence: async () => ({ items: [{ topic_id: 't1', kind: 'objective_answer', score: 1, source_refs: [ref] }], next_cursor: null }), plan: async () => structuredClone(plan),
    health: async () => ({ status: 'ok', dependencies: { mysql: 'ok', llm: { status: 'configured', provider: 'test' } } }), waitRun: async () => ({ status: 'succeeded' }),
  };
}
function environment(route, overrides = {}, extras = {}) {
  let current = { mode: 'live', route };
  const commits = [], navigations = [];
  const api = { ...fixtures(), ...overrides }, storage = extras.storage || memory();
  const controller = createWorkspaceController({ api, context: () => current, storage, committed: async (...args) => commits.push(args), navigate: (...args) => navigations.push(args), ...extras });
  return { controller, storage, commits, navigations, api, change: route => { current = { ...current, route }; controller.sync(); } };
}

test('manual completion passes its receipt and original navigation version without rereading the task', async () => {
 let reads = 0, release, navigationVersion = 7, change;
 const receipt = { id: 'task1', space_id: 's1', plan_id: 'p1', plan_version: 5, status: 'completed', completion_receipt: { event_id: 'event' }, next_action: { kind: 'summary' } };
 const env = environment('task/p1/task1', { plan: async () => { reads++; return structuredClone(plan); }, updateTask: () => new Promise(resolve => { release = resolve; }) },
   { context: () => ({ route: 'task/p1/task1', navigationVersion }), committed: async (_, __, value) => { change = value; } });
 await env.controller.load(); const operation = env.controller.submit(values({ status: 'completed', note: '保留备注' }));
 navigationVersion = 9; release(receipt); await operation;
 assert.equal(change.completionReceipt, receipt); assert.equal(change.navigationVersion, 7); assert.equal(reads, 1);
});

test('old-parser imports expose explicit upgrade and inspectable uncovered pages', async () => {
  const lifecycle = { stage: 'learnable', parser_version: 'knowpath_parser_v1', parser_upgrade_available: true,
    parse_manifest: { complete: false, pages: [{ page: 2, status: 'ocr_required' }, { page: 3, status: 'known_blank' }, { page: 4, status: 'extraction_failed' }, { page: 5, status: 'text_with_images' }, { page: 6, status: 'text_with_graphics' }], uncovered_pages: [2, 4, 5, 6] } };
  const env = environment('material/m1', { materials: async () => [{ ...material, lifecycle }] });
  await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /已有解析版本/);
  assert.match(html, /新版解析器/);
  assert.match(html, /重新解析并审核/);
  assert.match(html, /material-reparse/);
  assert.match(html, /第 2 页/);
  assert.match(html, /OCR/);
  assert.match(html, /第 4 页/);
  assert.match(html, /提取失败/);
  assert.match(html, /文字与图像/);
  assert.match(html, /文字与矢量图形，需核对图形内容/);
  assert.match(html, /已知空白/);
});

test('graph candidate preview explains containers objectives scope and optional content', async () => {
  const unit = { ...topic, kind: 'chapter', content_role: 'container', is_learnable: false, scope_summary: '限定的来源范围', learning_objectives: ['解释样本边界'] };
  const env = environment('material-graph/m1', { graphDiff: async () => ({ ...diff, added: [{ kind: 'node', after: unit }, { kind: 'node', after: { ...unit, content_role: 'optional_summary', name: '总结' } }] }) });
  await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /章节容器/);
  assert.match(html, /不安排学习任务/);
  assert.match(html, /限定的来源范围/);
  assert.match(html, /解释样本边界/);
  assert.match(html, /可选总结/);
});

test('every dedicated page loads and renders with escaped backend content and one form at most', async () => {
  for (const page of Object.keys(workspaceTitles)) {
    const id = page.startsWith('material') ? 'm1' : page.startsWith('assessment') ? 'a1' : ['task', 'session'].includes(page) ? 'p1' : 's1';
    const item = page === 'assessment-resolve' ? 'r1' : page.startsWith('assessment') ? 'q1' : 'task1';
    const { controller } = environment(`${page}/${id}/${item}`);
    await controller.load(); assert.equal(controller.snapshot().error, '', page);
    const html = renderWorkspace(controller.snapshot());
    assert.ok(html.includes(workspaceTitles[page]), page);
    assert.ok(!html.includes('<img onerror=alert(1)>'), page);
    assert.ok(!html.includes('<script>反馈</script>'), page);
    assert.ok((html.match(/data-workspace-form/g) || []).length <= 1, page);
  }
});
test('material management reads metadata without disclosing source text', async () => {
  const { controller } = environment('material/m1', { material: async () => assert.fail('should not read source chunks') });
  await controller.load(); assert.equal(controller.snapshot().data.affected.length, 1);
});
test('material recovery menu offers retry only for a known retryable failed lifecycle', async () => {
  for (const lifecycle of [undefined, { stage: 'received' }, { stage: 'parsing' }, { stage: 'preparing' }, { stage: 'pending_review' }, { stage: 'learnable' }, { stage: 'failed', error: { retryable: false } }, { stage: 'failed', error: { details: { retryable: true } } }]) {
    const env = environment('material/m1', { materials: async () => [{ ...material, lifecycle }] });
    await env.controller.load();
    const html = renderWorkspace(env.controller.snapshot());
    assert.equal(html.includes('重试失败任务'), lifecycle?.stage === 'failed' && !!lifecycle.error?.details?.retryable);
    assert.match(html, /重新解析原文件/);
  }
});
test('deletion requires both impact acknowledgement and explicit confirmation before making any call', async () => {
  let body, writes = 0;
  const env = environment('material-delete/m1', { deleteMaterial: async (id, payload) => { body = payload; writes++; return { run_id: 'delete', status: 'queued' }; } });
  await env.controller.load();
  await env.controller.submit(values({ cascade: 'on' })); assert.equal(writes, 0);
  await env.controller.submit(values({ confirm: 'on' })); assert.equal(writes, 0);
  await env.controller.submit(values({ confirm: 'on', cascade: 'on' }));
  assert.deepEqual(body, { expected_version: 3, confirm: true, cascade: true });
  assert.equal(env.commits.length, 1); assert.equal(env.navigations[0][0], '#/materials');
});
test('failed runs never emit success or clear affected material data', async () => {
  const env = environment('material-delete/m1', { deleteMaterial: async () => ({ run_id: 'delete' }), waitRun: async () => { throw new ApiError('无法删除', 200, 'RUN_FAILED', { runStatus: 'failed' }); } });
  await env.controller.load(); await env.controller.submit(values({ confirm: 'on', cascade: 'on' }));
  assert.equal(env.commits.length, 0); assert.equal(env.navigations.length, 0); assert.equal(env.controller.snapshot().notice, ''); assert.match(env.controller.snapshot().error, /无法删除/);
});
test('a changed set of referencing spaces requires a new impact confirmation', async () => {
  let reads = 0, writes = 0;
  const env = environment('material-delete/m1', { spaces: async () => ++reads === 1 ? [space] : [space, { ...space, id: 's2', name: '新空间' }], deleteMaterial: async () => { writes++; } });
  await env.controller.load(); await env.controller.submit(values({ confirm: 'on', cascade: 'on' }));
  assert.equal(writes, 0); assert.match(env.controller.snapshot().error, /关联空间已变化/);
});
test('a queued deletion remains pollable after reload even when the material record has disappeared', async () => {
  const storage = memory(); let polls = 0;
  const api = { deleteMaterial: async () => ({ run_id: 'delete-run' }), waitRun: async () => { if (++polls === 1) throw new ApiError('后台处理中', 0, 'RUN_PENDING'); return { status: 'succeeded' }; } };
  let env = environment('material-delete/m1', api, { storage }); await env.controller.load(); await env.controller.submit(values({ confirm: 'on', cascade: 'on' }));
  env = environment('material-delete/m1', { ...api,
    materials: async () => [],
    materialVersions: async () => [],
  }, { storage }); await env.controller.load();
  assert.equal(env.controller.snapshot().data, null); assert.equal(env.controller.snapshot().pendingRun, 'delete-run');
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /查看原任务结果/);
  assert.match(html, /删除请求已受理/);
  assert.match(html, /清理/);
  assert.doesNotMatch(html, /资料不存在|暂未读取到内容/);
  await env.controller.action('poll-command'); assert.equal(env.navigations[0][0], '#/materials');
});
test('knowledge updates replace only selected material bindings and strip internal fields', () => {
  const preview = { bindings: space.bindings.map(row => ({ ...row, topic_revision_ids: { t1: 'r' } })), available_updates: [{ material_id: 'm1', material_version_id: 'v3', graph_version: 4, graph_revision_id: 'gr4' }] };
  assert.deepEqual(updatedBindings(preview, ['m1']), [{ material_id: 'm1', material_version_id: 'v3', graph_version: 4 }, space.bindings[1]]);
  assert.throws(() => updatedBindings(preview, [])); assert.throws(() => updatedBindings(preview, ['unbound'])); assert.throws(() => updatedBindings(preview, ['m1', 'm1']));
});
test('timed-out knowledge update survives reload and polls its original run without resubmission', async () => {
  const storage = memory(); let writes = 0, polls = 0;
  const api = { applyKnowledgeUpdates: async () => { writes++; return { run_id: 'apply', status: 'queued' }; }, waitRun: async id => { assert.equal(id, 'apply'); polls++; if (polls === 1) throw new ApiError('仍在处理', 0, 'RUN_PENDING'); return { status: 'succeeded' }; } };
  let env = environment('knowledge-updates/s1', api, { storage });
  await env.controller.load(); await env.controller.submit(values({ confirm: 'on', materials: 'm1' })); assert.equal(env.commits.length, 0);
  env = environment('knowledge-updates/s1', api, { storage }); await env.controller.load(); await env.controller.submit(values({ confirm: 'on', materials: 'm1' }));
  assert.equal(writes, 1); assert.equal(polls, 2); assert.equal(env.commits.length, 1);
});
test('ambiguous POST reuses immutable body and idempotency key after reload', async () => {
  const calls = [], storage = memory();
  const api = { gradeReview: async (id, body, options) => { calls.push({ id, body, key: options.key }); if (calls.length === 1) throw new ApiError('网络中断', 0, 'NETWORK'); return { run_id: 'grade' }; } };
  let env = environment('assessment-grade/a1/q1', api, { storage }); await env.controller.load(); await env.controller.submit(values({ reason: '有同义答案' }));
  env = environment('assessment-grade/a1/q1', api, { storage }); await env.controller.load();
  await env.controller.submit(values({ reason: '修改内容' })); assert.equal(calls.length, 1);
  await env.controller.submit(values({ reason: '有同义答案' })); assert.deepEqual(calls[0], calls[1]); assert.equal(env.commits.length, 1);
});

test('manual task completion refreshes the same task after a version conflict and waits for reconfirmation', async () => {
  const calls = []; let reads = 0;
  const latest = { ...plan, version: 5, tasks: [{ id: 'other', status: 'pending' }, { ...plan.tasks[0], context: { title: '刷新后的原任务' } }] };
  const api = { plan: async () => { reads++; return structuredClone(reads === 1 ? plan : latest); },
    updateTask: async (id, item, body, options) => { calls.push({ id, item, body, key: options.key }); if (calls.length === 1) throw new ApiError('版本变化', 409, 'VERSION_CONFLICT'); return { id: item, status: 'completed', plan_version: 6 }; } };
  const env = environment('task/p1/task1', api);
  await env.controller.load(); await env.controller.submit(values({ status: 'completed', note: '确认原任务' }));
  assert.equal(calls.length, 1); assert.equal(reads, 2); assert.equal(env.commits.length, 0);
  assert.equal(env.controller.snapshot().data.plan.version, 5); assert.equal(env.controller.snapshot().data.task.id, 'task1');
  assert.match(renderWorkspace(env.controller.snapshot()), /刷新后的原任务/);
  assert.match(env.controller.snapshot().error, /再次确认/);
  await env.controller.submit(values({ status: 'completed', note: '确认原任务' }));
  assert.equal(calls.length, 2); assert.equal(calls[1].item, 'task1'); assert.equal(calls[1].body.expected_plan_version, 5);
  assert.notEqual(calls[1].key, calls[0].key); assert.equal(env.commits.length, 1);
});

test('task commits identify their target space, changed projections and acknowledged version', async () => {
  let current = structuredClone(plan);
  const env = environment('task/p1/task1', { plan: async () => structuredClone(current), updateTask: async () => {
    current = { ...current, version: 5, tasks: [{ ...current.tasks[0], status: 'completed' }] };
    return { ...current.tasks[0], plan_version: 5 };
  } });
  await env.controller.load(); await env.controller.submit(values({ status: 'completed' }));
  const change = env.commits[0][2];
  assert.equal(change?.spaceId, 's1'); assert.deepEqual(change.resources, ['workbench', 'progress', 'plan']);
  assert.equal(change.planId, 'p1'); assert.equal(change.planVersion, 5);
});

test('task acknowledgement remains visible when projection refresh and detail reread both fail', async () => {
  let reads = 0, writes = 0;
  const env = environment('task/p1/task1', { plan: async () => { if (++reads > 1) throw new Error('详情读取离线'); return structuredClone(plan); }, updateTask: async () => {
    writes++; return { ...plan.tasks[0], status: 'completed', note: '已确认备注', plan_version: 5 };
  } }, { committed: async () => { throw new Error('路线刷新离线'); } });
  await env.controller.load(); await env.controller.submit(values({ status: 'completed', note: '已确认备注' }));
  assert.equal(env.controller.snapshot().data.task.status, 'completed'); assert.equal(env.controller.snapshot().data.task.note, '已确认备注');
  assert.equal(env.controller.snapshot().data.plan.version, 5); assert.match(env.controller.snapshot().error, /刷新失败/);
  assert.equal(reads, 2); assert.equal(writes, 1);
});

test('progress recovery never clears an unrelated management validation error', async () => {
  const env = environment('task/p1/task1'); await env.controller.load(); await env.controller.submit(values({ status: 'skipped' }));
  const error = env.controller.snapshot().error; assert.match(error, /跳过原因/);
  await env.controller.action('refresh-committed', 's1'); assert.equal(env.controller.snapshot().error, error);
});

test('top refresh retains the acknowledged task until a matching non-regressing detail is read', async () => {
  let current = structuredClone(plan), writes = 0;
  const env = environment('task/p1/task1', { plan: async () => structuredClone(current), updateTask: async () => {
    writes++; return { ...plan.tasks[0], status: 'completed', plan_version: 5 };
  } }, { committed: async () => { throw new Error('路线刷新离线'); } });
  await env.controller.load(); await env.controller.submit(values({ status: 'completed' }));
  for (const invalid of [plan, { ...plan, id: 'foreign', version: 6 }, { ...plan, space_id: 'foreign', version: 6 }]) {
    current = invalid; await env.controller.action('reload');
    assert.equal(env.controller.snapshot().data.plan.version, 5);
    assert.equal(env.controller.snapshot().data.task.status, 'completed');
    assert.ok(env.controller.snapshot().refreshError); assert.match(env.controller.snapshot().error, /旧|不一致/);
  }
  current = { ...plan, version: 5, tasks: [{ ...plan.tasks[0], status: 'completed' }] };
  await env.controller.action('reload');
  assert.equal(env.controller.snapshot().error, ''); assert.equal(env.controller.snapshot().refreshError, undefined);
  assert.equal(env.controller.snapshot().data.plan.version, 5); assert.equal(writes, 1);
});

test('manual task completion replays its original note body and key after a 503 and a newer plan reload', async () => {
  const calls = [], storage = memory(); let current = structuredClone(plan);
  const api = { plan: async () => structuredClone(current), updateTask: async (id, item, body, options) => {
    calls.push({ id, item, body, key: options.key });
    if (calls.length === 1) { current = { ...plan, version: 5, tasks: [{ ...plan.tasks[0], status: 'completed', note: '封存原备注' }] }; throw new ApiError('响应未知', 503, 'UNAVAILABLE'); }
    return { id: item, status: 'completed', plan_version: 5 };
  } };
  let env = environment('task/p1/task1', api, { storage }); await env.controller.load();
  await env.controller.submit(values({ status: 'completed', note: '封存原备注' })); assert.equal(env.commits.length, 0);
  env = environment('task/p1/task1', api, { storage: { ...storage } }); await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot()); assert.match(html, /重试原完成确认/); assert.match(html, /封存原备注/);
  await env.controller.submit(values({ status: 'completed', note: '不能替换原意图' }));
  assert.equal(calls.length, 2); assert.deepEqual(calls[0], calls[1]); assert.equal(env.commits.length, 1);
  assert.equal(calls[1].body.note, '封存原备注'); assert.equal(calls[1].body.expected_plan_version, 4);
});

test('a completed task saves a new note intent while an unchanged acknowledged intent is replayed', async () => {
  const calls = []; let current = structuredClone(plan);
  const env = environment('task/p1/task1', { plan: async () => structuredClone(current), updateTask: async (id, item, body, options) => {
    calls.push({ id, item, body, key: options.key });
    current = { ...current, version: current.version + 1, tasks: [{ ...current.tasks[0], status: 'completed', note: body.note }] };
    return { ...current.tasks[0], plan_version: current.version };
  } });
  await env.controller.load();
  await env.controller.submit(values({ status: 'completed', note: 'first note' }));
  await env.controller.submit(values({ status: 'completed', note: 'edited note' }));
  assert.equal(calls.length, 2); assert.equal(calls[1].body.note, 'edited note'); assert.equal(calls[1].body.expected_plan_version, 5);
  assert.notEqual(calls[0].key, calls[1].key); assert.equal(env.controller.snapshot().data.task.note, 'edited note');
  await env.controller.submit(values({ status: 'completed', note: 'edited note' }));
  assert.equal(calls.length, 2); assert.equal(env.controller.snapshot().error, '');
});

test('an unknown note edit keeps its command even if the retry matches an older acknowledged note', async () => {
  const calls = []; let current = structuredClone(plan);
  const env = environment('task/p1/task1', { plan: async () => structuredClone(current), updateTask: async (id, item, body, options) => {
    calls.push({ id, item, body, key: options.key });
    if (calls.length === 2) throw new ApiError('备注保存结果未知', 503, 'UNAVAILABLE');
    current = { ...current, version: current.version + 1, tasks: [{ ...current.tasks[0], status: 'completed', note: body.note }] };
    return { ...current.tasks[0], plan_version: current.version };
  } });
  await env.controller.load(); await env.controller.submit(values({ status: 'completed', note: '旧备注' }));
  await env.controller.submit(values({ status: 'completed', note: '待确认的新备注' }));
  assert.match(env.controller.snapshot().error, /结果未知/);
  await env.controller.submit(values({ status: 'completed', note: '旧备注' }));
  assert.equal(calls.length, 3); assert.deepEqual(calls[1], calls[2]); assert.equal(env.controller.snapshot().data.task.note, '待确认的新备注');
});

test('late page loads and mutations cannot replace a different route or redirect it', async () => {
  let resolveRead, resolveWrite;
  const env = environment('material/m1', { materialVersions: () => new Promise(resolve => { resolveRead = resolve; }) });
  const loading = env.controller.load(); env.change('health'); await env.controller.load(); resolveRead([]); await loading;
  assert.equal(env.controller.snapshot().route.page, 'health'); assert.ok(env.controller.snapshot().data.health);
  const second = environment('material-delete/m1', { deleteMaterial: () => new Promise(resolve => { resolveWrite = resolve; }) });
  await second.controller.load(); const submit = second.controller.submit(values({ confirm: 'on', cascade: 'on' }));
  second.change('health'); await second.controller.load(); resolveWrite({ status: 'succeeded' }); await submit;
  assert.ok(second.controller.snapshot().data.health); assert.equal(second.navigations.length, 0);
});
test('question adjudication uses exact frozen references and requires source confirmation', () => {
  const base = { confirm: 'on', action: 'correct', reason: '核对原文', source: '0', answer_key: 'B', rubric: '完整新标准' };
  assert.deepEqual(resolutionPayload(values(base), review, 5), { action: 'correct', expected_review_version: 5, confirmed: true, reason: '核对原文', source_refs: [ref], corrected_answer_key: 'B', corrected_rubric: '完整新标准' });
  for (const patch of [{ confirm: '' }, { source: '9' }, { answer_key: 'C' }, { rubric: '' }]) assert.throws(() => resolutionPayload(values({ ...base, ...patch }), review, 5));
  const invalidate = resolutionPayload(values({ ...base, action: 'invalidate' }), review, 5);
  assert.ok(!('corrected_answer_key' in invalidate)); assert.ok(!('corrected_rubric' in invalidate));
});
test('results do not automatically open frozen review source text', async () => {
  const env = environment('assessment-result/a1', { questionReviews: async () => assert.fail('source disclosure must be explicit') });
  await env.controller.load(); assert.equal(env.controller.snapshot().error, '');
  assert.match(renderWorkspace(env.controller.snapshot()), /任意映射/);
});
test('report and resolve carry the current review version and wait for grading', async () => {
  let reportBody, resolveBody;
  const env = environment('assessment-report/a1/q1', { reportQuestion: async (id, body) => { reportBody = body; return { run_id: 'r', status: 'pending' }; } });
  await env.controller.load(); await env.controller.submit(values({ reason: '题意不清' })); assert.deepEqual(reportBody, { question_id: 'q1', reason: '题意不清', expected_review_version: 5 });
  const second = environment('assessment-resolve/a1/r1', { resolveQuestion: async (id, rid, body) => { assert.equal(rid, 'r1'); resolveBody = body; return { run_id: 'rr', status: 'invalid' }; } });
  await second.controller.load(); await second.controller.submit(values({ confirm: 'on', source: '0', action: 'invalidate', reason: '依据不支持' })); assert.equal(resolveBody.expected_review_version, 5); assert.equal(second.commits.length, 1);
});
test('knowledge correction creates a draft and confirms it on its own page', async () => {
  let creates = 0, confirms = 0;
  const env = environment('knowledge-correct/s1', { createCorrection: async (id, body) => { creates++; assert.equal(body.source_ref, 'c1'); return { correction_id: 'fix1', candidate_revision_id: 'revision1', status: 'pending' }; }, graphDiff: async () => ({ ...diff, status: 'draft' }), confirmCorrection: async (id, correctionId, body) => { confirms++; assert.equal(correctionId, 'fix1'); assert.equal(body.expected_graph_version, 2); return { run_id: 'fixrun' }; } });
  await env.controller.load(); await env.controller.submit(values({ target: 'node:t1', action: 'replace', source: 'c1', name: '更正名称', reason: '原文定义' }));
  assert.equal(creates, 1); assert.equal(confirms, 0); assert.match(renderWorkspace(env.controller.snapshot()), /确认发布纠错/);
  await env.controller.submit(values({ confirm: 'on', reason: '已核对' })); assert.equal(confirms, 1); assert.equal(env.controller.snapshot().data.correction, null);
});
test('candidate installation invalidates a creation form rendered during committed refresh', async () => {
  let controller, mounted = null, status = 'draft';
  const commands = [];
  const rendered = [];
  function render() {
    const snapshot = controller.snapshot(), html = renderWorkspace(snapshot);
    const key = /data-workspace-key="([^"]+)"/.exec(html)?.[1];
    const form = /<form data-workspace-form>[\s\S]*?<\/form>/.exec(html)?.[0] || '';
    // Match app.render's keyed DOM preservation, including intermediate renders
    // made by committed() before finish() installs the freshly read candidate.
    mounted = { key, form: mounted?.key === key && mounted.form && form ? mounted.form : form };
    rendered.push({ key, correction: snapshot.data?.correction?.correction_id, form: mounted.form });
  }
  const api = { createCorrection: async (id, body, options) => { commands.push({ body, key: options.key }); status = 'draft'; return { correction_id: 'fix-' + commands.length, candidate_revision_id: 'revision-' + commands.length }; }, graphDiff: async () => ({ ...diff, status }) };
  const env = environment('knowledge-correct/s1', api, { changed: () => { if (controller) render(); }, committed: async () => { render(); } });
  controller = env.controller; await controller.load();
  const before = mounted.form; await controller.submit(values({ source: 'invalid', reason: '' }));
  assert.equal(mounted.form, before, 'same-form validation renders preserve the mounted form');
  await controller.submit(values({ target: 'node:t1', action: 'replace', source: 'c1', name: '更正后', reason: '核对来源' }));
  const intermediate = rendered.findLast(row => !row.correction);
  assert.ok(intermediate?.form.includes('生成纠错候选'));
  assert.match(mounted.form, /name="confirm"/);
  assert.match(mounted.form, /确认发布纠错/);
  assert.notEqual(mounted.key, intermediate.key);
  status = 'superseded'; await controller.load(true);
  const terminalKey = mounted.key; assert.equal(mounted.form, '');
  await controller.action('restart-correction');
  assert.notEqual(mounted.key, terminalKey); assert.match(mounted.form, /name="target"/); assert.doesNotMatch(mounted.form, /name="confirm"/);
  const editorKey = mounted.key;
  await controller.submit(values({ target: 'node:t1', action: 'replace', source: 'c1', name: '更正后', reason: '核对来源' }));
  assert.notEqual(mounted.key, editorKey); assert.match(mounted.form, /name="confirm"/); assert.doesNotMatch(mounted.form, /name="target"/);
  assert.equal(commands.length, 2); assert.deepEqual(commands[1].body, commands[0].body); assert.notEqual(commands[1].key, commands[0].key);
});
for (const status of ['failed', 'superseded']) test(`terminal ${status} correction preserves content and starts an explicit new intent`, async () => {
  const storage = memory(), calls = []; let currentStatus = status, graphReads = 0;
  const body = { kind: 'node', target_id: 't1', action: 'replace', source_ref: 'c1', reason: '保留的原因', proposed_value: { name: '保留的名称', description: '保留的解释' } };
  const descriptor = { correction_id: 'old-fix', candidate_revision_id: 'old-revision', materialId: 'm1', body };
  storage.setItem('knowpath-pending-corrections-v1', JSON.stringify({ s1: descriptor }));
  const api = { graph: async () => { graphReads++; return { nodes: [topic], edges: [] }; }, graphDiff: async (id, revision) => ({ ...diff, candidate_revision_id: revision, status: revision === 'old-revision' ? currentStatus : 'draft', error: { message: '准备失败', details: { stage: 'preparing', retryable: true } } }), createCorrection: async (id, payload, options) => { calls.push({ payload, key: options.key }); return { correction_id: 'new-fix', candidate_revision_id: 'new-revision' }; }, confirmCorrection: async () => assert.fail('terminal candidate cannot be confirmed') };
  let env = environment('knowledge-correct/s1', api, { storage }); await env.controller.load();
  let html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /重新提交纠错/); assert.match(html, /保留的名称/);
  assert.match(html, /目标：知识点 · 线性变换 · 更正/);
  assert.match(html, /名称：保留的名称/); assert.match(html, /解释：保留的解释/);
  assert.match(html, /data-workspace-action="source"/); assert.match(html, /来源 1 · 第 2 页/);
  assert.doesNotMatch(html, /目标：node|>name：|>description：|原文依据：c1/);
  assert.doesNotMatch(html, /候选正在准备或已处理|确认发布纠错/);
  if (status === 'failed') { assert.match(html, /准备失败/); assert.match(html, /role="alert"/); }
  await env.controller.submit(values({ confirm: 'on', reason: '不得确认旧候选' })); assert.equal(calls.length, 0);
  await env.controller.action('restart-correction'); assert.equal(calls.length, 0); assert.ok(graphReads > 0);
  assert.deepEqual(env.controller.snapshot().data.correctionDraft, body);
  env = environment('knowledge-correct/s1', api, { storage }); await env.controller.load();
  html = renderWorkspace(env.controller.snapshot()); assert.match(html, /保留的解释/); assert.match(html, /保留的原因/); assert.doesNotMatch(html, /name="confirm"/);
  await env.controller.submit(values({ target: 'node:t1', action: 'replace', source: 'c1', name: '保留的名称', description: '保留的解释', reason: '保留的原因' }));
  assert.equal(calls.length, 1); assert.deepEqual(calls[0].payload, body);
  const saved = JSON.parse(storage.getItem('knowpath-pending-corrections-v1')).s1;
  assert.equal(saved.correction_id, 'new-fix'); assert.equal(saved.history[0].correction_id, 'old-fix'); assert.deepEqual(saved.history[0].body, body);
});
test('restarting a correction requires reselecting a missing current target or source', async () => {
  const storage = memory(), body = { kind: 'node', target_id: 't1', action: 'replace', source_ref: 'c1', proposed_value: { name: '原提议' }, reason: '原原因' };
  storage.setItem('knowpath-pending-corrections-v1', JSON.stringify({ s1: { correction_id: 'old', candidate_revision_id: 'old-r', materialId: 'm1', body } }));
  let graph = { nodes: [{ ...topic, source_refs: [{ ...ref, chunk_id: 'current-source' }] }], edges: [] };
  const env = environment('knowledge-correct/s1', { graph: async () => graph, graphDiff: async () => ({ ...diff, status: 'superseded' }), createCorrection: async () => assert.fail('stale source cannot be submitted') }, { storage });
  await env.controller.load(); await env.controller.action('restart-correction');
  assert.match(renderWorkspace(env.controller.snapshot()), /来源.*重新选择/);
  await env.controller.submit(values({ target: 'node:t1', source: 'c1', action: 'replace', name: '原提议', reason: '原原因' }));
  assert.match(env.controller.snapshot().error, /原文依据/);
  graph = { nodes: [], edges: [] }; await env.controller.load(true);
  assert.match(renderWorkspace(env.controller.snapshot()), /目标.*重新选择/);
  assert.match(renderWorkspace(env.controller.snapshot()), /原提议/);
});
test('accepted correction confirmation resumes its original run while preparation is pending', async () => {
  const storage = memory(); let status = 'draft', confirmations = 0, polls = 0;
  storage.setItem('knowpath-pending-corrections-v1', JSON.stringify({ s1: { correction_id: 'fix', candidate_revision_id: 'revision1', materialId: 'm1', body: { reason: '原纠错' } } }));
  const api = { graphDiff: async () => ({ ...diff, status }), confirmCorrection: async () => { confirmations++; status = 'pending_review'; return { run_id: 'original-run' }; }, waitRun: async id => { assert.equal(id, 'original-run'); if (++polls === 1) throw new ApiError('仍在准备', 0, 'RUN_PENDING'); return { status: 'succeeded' }; } };
  let env = environment('knowledge-correct/s1', api, { storage }); await env.controller.load();
  await env.controller.submit(values({ confirm: 'on', reason: '重新核对' }));
  env = environment('knowledge-correct/s1', api, { storage }); await env.controller.load();
  await env.controller.submit(values({ confirm: 'on', reason: '重新核对' }));
  assert.equal(confirmations, 1); assert.equal(polls, 2); assert.equal(env.controller.snapshot().data.correctionPublished, true);
});
test('node and relation replacements cannot invent a source, and rejection omits replacement data', () => {
  assert.throws(() => correctionPayload(values({ action: 'replace', source: 'fake', reason: 'x', name: 'new' }), topic, 'node'));
  assert.deepEqual(correctionPayload(values({ action: 'reject', source: 'c1', reason: '缺少支持', name: 'ignored' }), topic, 'node'), { kind: 'node', target_id: 't1', action: 'reject', source_ref: 'c1', reason: '缺少支持' });
  const html = correctionFields({ graph: { nodes: [topic], edges: [] } }, 'node:t1'); assert.match(html, /更正后的名称/); assert.match(html, /原文依据/);
});
test('task postponement uses UTC and skip requires a reason', () => {
  const payload = taskPayload(values({ status: 'deferred', defer_until: '2099-01-02T12:00', note: '复习' }), plan);
  assert.equal(payload.expected_plan_version, 4); assert.match(payload.defer_until, /Z$/);
  assert.throws(() => taskPayload(values({ status: 'skipped' }), plan));
  assert.ok(!('defer_until' in taskPayload(values({ status: 'completed', defer_until: '2099-01-02T12:00' }), plan)));
});
test('real study session survives navigation, records pause and finish, and never claims focus duration', async () => {
  const calls = [];
  const env = environment('session/p1/task1', { startSession: async () => ({ session_id: 'ss1', status: 'active', task_id: 'task1', plan_id: 'p1' }), sessionEvent: async (id, body) => { calls.push({ id, body }); return {}; }, finishSession: async () => ({ session_id: 'ss1', status: 'finished', elapsed_seconds: 123, reported_active_seconds: null }) });
  await env.controller.load(); await env.controller.action('session-start'); await env.controller.action('session-event', 'pause');
  assert.deepEqual(calls[0], { id: 'ss1', body: { type: 'pause', topic_id: 't1' } });
  env.change('health'); await env.controller.load(); env.change('session/p1/task1'); await env.controller.load(); assert.equal(env.controller.snapshot().data.session.paused, true);
  await env.controller.action('session-finish'); assert.match(renderWorkspace(env.controller.snapshot()), /包含暂停，不等同于专注时长/);
});
test('filters preserve date timezone and reject inverted ranges', () => {
  const range = dateRange(values({ from: '2026-01-01T10:00', to: '2026-02-01T12:00' })); assert.match(range.from, /Z$/);
  assert.throws(() => dateRange(values({ from: '2026-02-01T10:00', to: '2026-01-01T12:00' })));
});
test('evidence pagination preserves the active filters', async () => {
  const calls = [];
  const env = environment('evidence/s1', { evidence: async (id, filters) => { calls.push(filters); return { items: [{ id: filters.cursor ? 'e2' : 'e1' }], next_cursor: filters.cursor ? null : 'cursor1' }; } });
  await env.controller.load(); await env.controller.filter(values({ topic_id: 't1', kind: 'short_answer' })); await env.controller.action('more');
  assert.deepEqual(calls.at(-1), { topic_id: 't1', kind: 'short_answer', status: '', cursor: 'cursor1' });
  assert.equal(env.controller.snapshot().data.evidence.items.length, 2);
});
test('state reset sends only selected topics with the current state version', async () => {
  let body;
  const env = environment('learning-reset/s1', { resetState: async (id, payload) => { body = payload; return {}; } });
  await env.controller.load(); await env.controller.submit(values({ topic_ids: ['t1'], confirm: 'on', reason: '重新学习' }));
  assert.deepEqual(body, { topic_ids: ['t1'], expected_state_version: 3, reason: '重新学习' });
});
test('exports wait for success and download authenticated binary response', async () => {
  const calls = [], downloads = [];
  const env = environment('export/s1', { exportSpace: async () => { calls.push('create'); return { run_id: 'exportRun', export_id: 'zip1' }; }, waitRun: async () => calls.push('wait'), downloadExport: async id => { calls.push(id); return new Response(new Blob(['zip'])); } }, { download: (...args) => downloads.push(args) });
  await env.controller.load(); await env.controller.action('export'); assert.deepEqual(calls, ['create', 'wait', 'zip1']); assert.equal(await downloads[0][0].text(), 'zip');
});
test('profile editing saves weekly budget and clears the target date with the current version', async () => {
  let body;
  const env = environment('space-profile/s1', { updateProfile: async (id, payload) => { body = payload; return {}; } });
  await env.controller.load(); await env.controller.submit(values({ goal: '准备期末考试', weekly_minutes: '120', target_date: '', example_first: 'on' }));
  assert.deepEqual(body, { goal: '准备期末考试', preferences: { example_first: true, concise_explanations: false }, weekly_minutes: 120, target_date: null, expected_version: 2 });
});

test('personalization controls default on and preserve unrelated preferences when saved', async () => {
  let body;
  const env = environment('space-profile/s1', {
    profile: async () => ({ profile_version: 3, profile: { preferences: { value: { lesson_style: 'concise', automatic_adjustment: true } } } }),
    updateProfile: async (id, payload) => { body = payload; return {}; },
  });
  await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /name="automatic_adjustment"[^>]+checked/);
  assert.match(html, /name="time_personalization"[^>]+checked/);
  assert.match(html, /name="lesson_adaptation"[^>]+checked/);
  await env.controller.submit(values({ goal: '理解资料', personalization_controls: '1', time_personalization: 'on' }));
  assert.deepEqual(body.preferences, { lesson_style: 'concise', example_first: false, concise_explanations: false, automatic_adjustment: false, time_personalization: true, lesson_adaptation: false });
  assert.equal(body.expected_version, 3);
});

test('optional profile budget stays unset and invalid budgets or dates never reach PATCH', async () => {
  const calls = [];
  const env = environment('space-profile/s1', { updateProfile: async (id, body) => { calls.push(body); return {}; } });
  await env.controller.load();
  for (const weekly_minutes of ['14', '2401', '12.5', 'invalid']) {
    await env.controller.submit(values({ goal: 'Goal', weekly_minutes, target_date: '' }));
    assert.equal(calls.length, 0);
  }
  await env.controller.submit(values({ goal: 'Goal', weekly_minutes: '', target_date: '2026-02-30' }));
  assert.equal(calls.length, 0);
  await env.controller.submit(values({ goal: 'Goal', weekly_minutes: '', target_date: '2026-12-31' }));
  assert.ok(!('weekly_minutes' in calls[0])); assert.equal(calls[0].target_date, '2026-12-31');
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /name="weekly_minutes"[^>]*value="150"[^>]*min="15"[^>]*max="2400"/);
  assert.match(html, /name="target_date"/);
});

test('workspace sections and backlink keep management pages in their owning space section', async () => {
  assert.equal(typeof workspaceModule.workspaceSection, 'function');
  const sections = { task: 'plan', session: 'plan', 'assessment-result': 'assessment', 'space-scope': 'materials', 'knowledge-updates': 'materials', 'knowledge-correct': 'materials', 'knowledge-changes': 'materials', 'learning-state': 'review', evidence: 'review', 'learning-reset': 'review', 'space-profile': 'settings', 'space-name': 'settings', 'space-settings': 'settings', export: 'settings' };
  for (const [page, section] of Object.entries(sections)) {
    assert.equal(workspaceModule.workspaceSection(page), section);
    const env = environment(`${page}/${page.startsWith('assessment') ? 'a1' : ['task', 'session'].includes(page) ? 'p1/task1' : 's1'}`);
    await env.controller.load();
    assert.match(renderWorkspace(env.controller.snapshot()), new RegExp(`back-link" href="#/space/s1/${section}"`));
    assert.match(renderWorkspace(env.controller.snapshot(), { returnPath: '#/space/s1/overview' }), /back-link" href="#\/space\/s1\/overview"/);
  }
});

test('space settings offers names preferences exports and reset while section pages hold learning tools', async () => {
  const env = environment('space-settings/s1'); await env.controller.load();
  const html = renderWorkspace(env.controller.snapshot());
  for (const page of ['space-name', 'space-profile', 'export', 'learning-reset']) assert.match(html, new RegExp(`#/manage/${page}/s1`));
  for (const page of ['space-scope', 'knowledge-updates', 'knowledge-changes', 'learning-state', 'evidence']) assert.doesNotMatch(html, new RegExp(`#/manage/${page}/s1`));
});

test('server workbench restores an active session and overrides a stale local descriptor', async () => {
  const storage = memory();
  storage.setItem('knowpath-study-sessions-v1', JSON.stringify({ s1: { session_id: 'stale', status: 'active', plan_id: 'p1', task_id: 'task1' } }));
  const env = environment('session/p1/task1', { workbench: async id => ({ space_id: id, active_session: { id: 'server-session', status: 'active', space_id: id, plan_id: 'p2', task_id: 'task2', paused: true } }) }, { storage });
  await env.controller.load();
  assert.equal(env.controller.snapshot().data.session.session_id, 'server-session');
  assert.match(renderWorkspace(env.controller.snapshot()), /href="#\/manage\/session\/p2\/task2"/);
  const noSession = environment('session/p1/task1', { workbench: async id => ({ space_id: id, active_session: null }) }, { storage });
  await noSession.controller.load(); assert.equal(noSession.controller.snapshot().data.session, null);
});

test('a mismatched server workbench never displays the local or foreign active session', async () => {
  const storage = memory();
  storage.setItem('knowpath-study-sessions-v1', JSON.stringify({ s1: { session_id: 'local', status: 'active', plan_id: 'p1', task_id: 'task1' } }));
  for (const workbench of [
    { space_id: 's2', active_session: null },
    { space_id: 's1', active_session: { session_id: 'foreign', space_id: 's2', plan_id: 'p1', task_id: 'task1', status: 'active' } },
  ]) {
    const env = environment('session/p1/task1', { workbench: async () => workbench }, { storage });
    await env.controller.load(); assert.equal(env.controller.snapshot().data, null); assert.match(env.controller.snapshot().error, /空间不一致/);
  }
});

test('session support navigation retains learning association and return route', async () => {
  const env = environment('session/p1/task1', { workbench: async () => ({ space_id: 's1', active_session: { session_id: 'ss1', status: 'active', space_id: 's1', plan_id: 'p1', task_id: 'task1' } }), sessionEvent: async () => ({}) });
  await env.controller.load(); await env.controller.action('session-event', 'request_hint');
  const [path, details] = env.navigations[0];
  assert.equal(path, '#/space/s1/assistant');
  assert.equal(details.learningSessionId, 'ss1'); assert.equal(details.planId, 'p1'); assert.equal(details.taskId, 'task1');
  assert.equal(details.returnTo, '#/manage/session/p1/task1'); assert.deepEqual(details.topicIds, ['t1']);
});

test('session support uses the sources sealed when the session started', async () => {
  const frozenTopic = { ...topic, name: '封存名称', source_refs: [{ ...ref, material_version_id: 'frozen-version', chunk_id: 'frozen-chunk' }] };
  const opened = [];
  const env = environment('session/p1/task1', { workbench: async () => ({ space_id: 's1', active_session: { session_id: 'ss1', status: 'active', space_id: 's1', plan_id: 'p1', task_id: 'task1', context: { topics: [frozenTopic] } } }), sessionEvent: async () => ({}) }, { openSource: async (...args) => opened.push(args) });
  await env.controller.load(); await env.controller.action('session-event', 'open_material');
  assert.equal(opened[0][0].chunk_id, 'frozen-chunk');
  await env.controller.action('session-event', 'request_explanation');
  assert.deepEqual(env.navigations[0][1].topics, [frozenTopic]);
});

test('finished session offers an associated assessment alongside task status editing', async () => {
  const env = environment('session/p1/task1', { startSession: async () => ({ session_id: 'ss1', status: 'active', task_id: 'task1', plan_id: 'p1' }), finishSession: async () => ({ session_id: 'ss1', status: 'finished', elapsed_seconds: 120 }) });
  await env.controller.load(); await env.controller.action('session-start'); await env.controller.action('session-finish');
  const html = renderWorkspace(env.controller.snapshot());
  assert.match(html, /data-action="task-assessment"[^>]*data-plan="p1"[^>]*data-task="task1"[^>]*data-session="ss1"/);
  assert.match(html, /data-topics="\[&quot;t1&quot;\]"/);
  assert.match(html, /href="#\/manage\/task\/p1\/task1"/);
});
test('scope changes do not allow the same topic in inclusion and exclusion', async () => {
  let calls = 0;
  const env = environment('space-scope/s1', { setScope: async (selected, included, excluded, prerequisites, options) => { calls++; assert.deepEqual(included, ['t1']); assert.deepEqual(excluded, []); assert.equal(prerequisites, false); assert.ok(options.key); return {}; } });
  await env.controller.load(); await env.controller.submit(values({ topic_ids: 't1', excluded_topic_ids: 't1' })); assert.equal(calls, 0);
  await env.controller.submit(values({ topic_ids: 't1' })); assert.equal(calls, 1);
});

test('scope command response cannot be downgraded by an older refresh projection', async () => {
  const env = environment('space-scope/s1', { setScope: async () => ({ topic_ids: ['t1'], excluded_topic_ids: [], include_prerequisites: false, space_version: 5, scope_version: 3 }) });
  await env.controller.load();
  await env.controller.submit(values({ topic_ids: ['t1'] }));
  assert.equal(env.controller.snapshot().data.space.space_version, 5);
  assert.equal(env.controller.snapshot().data.space.include_prerequisites, false);
  assert.match(env.controller.snapshot().notice, /学习范围已保存/);
});
test('new version upload sends the selected file and caches its exact version without waiting for auto-ingestion', async () => {
  let uploaded, cached;
  const env = environment('material-upload/m1', { uploadVersion: async (id, file, note, options) => { uploaded = { id, text: await file.text(), note, key: options.key }; return { material_version_id: 'v9', run_id: 'parse', status: 'uploaded' }; }, waitRun: async () => assert.fail('parse runs separately') }, { savePdf: async (file, materialId, versionId) => { cached = { materialId, versionId, text: await file.text() }; } });
  await env.controller.load(); const file = new File(['%PDF-1.7\nfixture'], 'version.pdf'); await env.controller.submit(values({ file, note: '修订章节' }));
  assert.equal(uploaded.id, 'm1'); assert.equal(uploaded.note, '修订章节'); assert.ok(uploaded.key); assert.deepEqual(cached, { materialId: 'm1', versionId: 'v9', text: '%PDF-1.7\nfixture' }); assert.match(env.controller.snapshot().notice, /新版本已上传/);
});
test('graph conflict publication requires a decision and reason for every conflict', async () => {
  let resolutions;
  const env = environment('material-graph/m1', { graphDiff: async () => ({ ...diff, conflicts: [{ conflict_id: 'conflict1', before: topic, after: topic }] }), publish: async (mid, rid, version, payload) => { assert.equal(version, 2); resolutions = payload; return { graph_version: 3 }; } });
  await env.controller.load(); await env.controller.submit(values({ confirm: 'on', 'conflict-0': 'keep_old' })); assert.equal(resolutions, undefined);
  await env.controller.submit(values({ confirm: 'on', 'conflict-0': 'keep_old', 'reason-0': '人工核对定义' })); assert.deepEqual(resolutions, [{ conflict_id: 'conflict1', action: 'keep_old', reason: '人工核对定义' }]);
});
test('API wrappers send exact routes, authentication, multipart versions and source-proof bodies', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/openapi.json')) return new Response(JSON.stringify({ paths: { '/api/v1/learning-spaces/{space_id}/messages': { post: { requestBody: { content: { 'application/json': { schema: { properties: { reading_context: {}, answer_mode: { enum: ['auto', 'material', 'general'] } } } } } } } } } }));
    calls.push({ url: new URL(url), options }); return new Response(JSON.stringify({ items: [], next_cursor: null }));
  });
  const api = createApi(() => 'fixture-token');
  await api.uploadVersion('m /', new File(['hello'], 'note.txt'), '版本说明', { key: 'upload-key' });
  assert.equal(calls[0].url.pathname, '/api/v1/materials/m%20%2F/versions'); assert.equal(calls[0].options.body.get('change_note'), '版本说明'); assert.equal(calls[0].options.headers['Idempotency-Key'], 'upload-key');
  await api.evidence('s1', { topic_id: '中 文', from: '2026-01-01T00:00:00Z', cursor: 'a&b' });
  assert.equal(calls[1].url.searchParams.get('cursor'), 'a&b'); assert.equal(calls[1].url.searchParams.get('topic_id'), '中 文');
  await api.publish('m1', 'gr1', 2, [{ conflict_id: 'c1', action: 'keep_old', reason: '已核对' }], { key: 'publish' });
  assert.equal(JSON.parse(calls[2].options.body).resolutions[0].reason, '已核对');
  await api.sendMessage('s1', '问题', null, ['m1']); assert.deepEqual(JSON.parse(calls[3].options.body).material_ids, ['m1']);
  for (const call of calls) assert.equal(call.options.headers['X-Local-Token'], 'fixture-token');
});
test('conditional form controls are disabled when hidden so unrelated required fields do not block submit', () => {
  const input = {}, root = { querySelectorAll: selector => selector === '[data-ws-when]' ? [{ dataset: { wsWhen: 'action:correct' }, closest: () => ({ elements: { namedItem: () => ({ value: 'invalidate' }) } }), querySelectorAll: () => [input] }] : [] };
  updateWorkspaceConditions(root, { busy: '' }); assert.equal(input.disabled, true);
});

test('historical task details are read-only and keep sealed names and notes', () => {
  const html = renderWorkspace({ mode: 'live', key: 'task', route: { page: 'task', id: 'p1', item: 'task1' }, data: { space, plan: { ...plan, status: 'superseded' }, task: { ...plan.tasks[0], note: '旧笔记', context: { historical: true, title: '封存标题' } } } });
  assert.match(html, /封存标题|旧笔记/);
  assert.doesNotMatch(html, /data-workspace-form|开始任务学习/);
});

test('completed task finished session offers retest without starting another session', () => {
  const html = renderWorkspace({ mode: 'live', key: 'session', route: { page: 'session', id: 'p1', item: 'task1' }, data: { space, plan: { ...plan, status: 'needs_replan', tasks: [{ ...plan.tasks[0], status: 'completed' }] }, task: { ...plan.tasks[0], status: 'completed' }, session: { id: 'session1', task_id: 'task1', plan_id: 'p1', status: 'finished', elapsed_seconds: 60 } } });
  assert.match(html, /data-action="task-assessment"[^>]*data-kind="retest"/);
  assert.doesNotMatch(html, /data-workspace-action="session-start"/);
});
test('finished management sessions keep source review path without blocked assessment', () => {
  const task={...plan.tasks[0],context:{topics:[{name:'图形',is_assessable:false,automatic_questions:false,source_refs:[{material_id:'m1'}]}]}};
  const html=renderWorkspace({mode:'live',key:'session',route:{page:'session',id:'p1',item:'task1'},data:{space,plan:{...plan,tasks:[task]},task,session:{id:'session1',task_id:'task1',plan_id:'p1',status:'finished'}}});
  assert.doesNotMatch(html,/data-action="task-assessment"/);
  assert.match(html,/需核对来源/);
  assert.match(html,/记录任务完成情况/);
  assert.match(html,/上传修正版本/);
});

test('management routes decode each identifier independently', () => {
  const path = workspaceModule.workspacePath('session', 'plan/a', '任务/b');
  assert.deepEqual(workspaceModule.workspaceRoute(path.slice('#/manage/'.length)), { page: 'session', id: 'plan/a', item: '任务/b' });
});

const undoPlan = () => ({ ...structuredClone(plan), tasks: [{ ...structuredClone(plan.tasks[0]), status: 'completed', note: 'old note', context: { completion: { event_id: 'e1', source: 'manual' } } }] });
test('undo completion requires reason and explicit confirmation; cancellation writes nothing', async () => {
  let writes = 0;
  const env = environment('task/p1/task1', { plan: async () => undoPlan(), undoTaskCompletion: async () => { writes++; } });
  await env.controller.load();
  assert.match(renderWorkspace(env.controller.snapshot()), /撤销本次任务完成/);
  await env.controller.action('undo-open');
  assert.match(renderWorkspace(env.controller.snapshot()), /撤销原因/);
  await env.controller.submit(values({ operation: 'undo_completion', reason: 'mistake' }));
  assert.equal(writes, 0);
  await env.controller.submit(values({ operation: 'undo_completion', confirm: 'on', reason: ' ' }));
  assert.equal(writes, 0);
  await env.controller.action('undo-cancel');
  assert.doesNotMatch(renderWorkspace(env.controller.snapshot()), /name="operation" value="undo_completion"/);
  assert.equal(writes, 0);
});
test('undo unknown response reload retries immutable event reason key and accepts lower count at higher version', async () => {
  const storage = memory(), calls = []; let current = undoPlan();
  const api = { plan: async () => structuredClone(current), undoTaskCompletion: async (p, t, body, options) => {
    calls.push({p, t, body:structuredClone(body), key:options.key});
    current = {...current, version: 5, tasks: [{...current.tasks[0],status:'pending'}]};
    if (calls.length === 1) throw new Error('response lost');
    return {...current.tasks[0],plan_id:'p1',plan_version:5, completed_count:0};
  }};
  const env = environment('task/p1/task1', api, {storage}); await env.controller.load(); await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion', reason:'wrong click',confirm:'on'}));
  assert.match(env.controller.snapshot().error,/response lost/);
  const restored = environment('task/p1/task1', api, {storage}); await restored.controller.load();
  assert.match(renderWorkspace(restored.controller.snapshot()), /重试原撤销/);
  await restored.controller.submit(values({operation:'undo_completion',reason:'edited',confirm:'on'}));
  assert.equal(calls.length,2); assert.deepEqual(calls[1],calls[0]);
  assert.equal(restored.controller.snapshot().data.task.status,'pending');
  assert.equal(restored.commits[0][2].planVersion,5);
});
test('undo 409 refreshes exact task and requires opening and confirming latest completion again', async () => {
  let current = undoPlan(), writes = 0;
  const env = environment('task/p1/task1', {plan:async()=>structuredClone(current),undoTaskCompletion:async()=>{
    writes++; current={...current,version:6,tasks:[{...current.tasks[0],context:{completion:{event_id:'e2'}}}]};
    throw new ApiError('changed',409,'VERSION_CONFLICT');
  }});
  await env.controller.load(); await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  assert.equal(writes,1); assert.equal(env.controller.snapshot().data.plan.version,6);
  assert.match(env.controller.snapshot().error,/再次/);
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  assert.equal(writes,1);
  await env.controller.action('undo-open'); assert.equal(env.controller.snapshot().taskUndo.completion_event_id,'e2');
});

test('successful undo clears prior unknown completion so same task can explicitly complete again', async () => {
  const storage=memory(); let current={...structuredClone(plan),tasks:[{...plan.tasks[0]}]}, calls=0;
  const api={plan:async()=>structuredClone(current),updateTask:async(p,t,body)=>{
    calls++; current={...current,version:body.expected_plan_version+1,tasks:[{...current.tasks[0],status:'completed',context:{completion:{event_id:`e${calls}`}}}]};
    if(calls===1) throw new Error('completion receipt lost');
    return {...current.tasks[0],plan_id:p,plan_version:current.version};
  },undoTaskCompletion:async(p,t,body)=>{
    current={...current,version:body.expected_plan_version+1,tasks:[{...current.tasks[0],status:'pending',context:{}}]};
    return {...current.tasks[0],plan_id:p,plan_version:current.version};
  }};
  const env=environment('task/p1/task1',api,{storage}); await env.controller.load();
  await env.controller.submit(values({status:'completed',note:'keep'})); assert.equal(calls,1);
  await env.controller.load(true); await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  await env.controller.submit(values({status:'completed',note:'new explicit completion'}));
  assert.equal(calls,2); assert.equal(env.controller.snapshot().data.plan.version,7);
  assert.equal(env.controller.snapshot().data.task.status,'completed');
});

test('old unknown undo receipt cannot replace a later completion or lower its acknowledged version', async () => {
  const storage=memory(); let current=undoPlan(), writes=0;
  const api={plan:async()=>structuredClone(current),undoTaskCompletion:async()=>{
    writes++;
    if(writes===1){current={...current,version:5,tasks:[{...current.tasks[0],status:'pending',context:{}}]};throw new Error('receipt lost');}
    return {...current.tasks[0],status:'pending',plan_version:5};
  }};
  let env=environment('task/p1/task1',api,{storage});await env.controller.load();await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  current={...current,version:6,tasks:[{...current.tasks[0],status:'completed',context:{completion:{event_id:'e2'}}}]};
  env=environment('task/p1/task1',api,{storage});await env.controller.load();
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  assert.equal(env.controller.snapshot().data.task.status,'completed');assert.equal(env.controller.snapshot().data.plan.version,6);
  assert.equal(env.commits[0][2].planVersion,6);
  assert.match(env.controller.snapshot().notice,/最新/);
});

test('unknown undo retains original retry even if the plan is later historical', async () => {
  const storage=memory();let current=undoPlan(),writes=0;
  const api={plan:async()=>structuredClone(current),undoTaskCompletion:async()=>{writes++;throw new Error('lost');}};
  let env=environment('task/p1/task1',api,{storage});await env.controller.load();await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  current={...current,status:'superseded',version:6};
  env=environment('task/p1/task1',api,{storage});await env.controller.load();
  assert.match(renderWorkspace(env.controller.snapshot()),/重试原撤销/);
  await env.controller.submit(values({operation:'undo_completion',reason:'edited',confirm:'on'}));assert.equal(writes,2);
});
test('history and archived task views cannot open a fresh undo', async () => {
  for(const [status,spaceStatus] of [['superseded','active'],['needs_replan','active'],['ready','archived']]){
    const current={...undoPlan(),status};
    const env=environment('task/p1/task1',{plan:async()=>current,space:async()=>({...space,status:spaceStatus})});
    await env.controller.load();await env.controller.action('undo-open');
    assert.doesNotMatch(renderWorkspace(env.controller.snapshot()),/data-workspace-action="undo-open"|name="operation" value="undo_completion"/);
  }
});

test('undo conflict refresh failure blocks a new confirmation until a successful read', async () => {
  let reads=0,writes=0,failRead=false;
  const env=environment('task/p1/task1',{plan:async()=>{reads++;if(failRead)throw new Error('offline');return undoPlan();},undoTaskCompletion:async()=>{writes++;failRead=true;throw new ApiError('changed',409,'VERSION_CONFLICT');}});
  await env.controller.load();await env.controller.action('undo-open');
  await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  assert.match(env.controller.snapshot().error,/刷新失败/);
  await env.controller.action('undo-open');await env.controller.submit(values({operation:'undo_completion',reason:'mistake',confirm:'on'}));
  assert.equal(writes,1);
  failRead=false;await env.controller.load(true);await env.controller.action('undo-open');
  assert.equal(env.controller.snapshot().taskUndo.open,true);assert.ok(reads>=3);
});

test('completed tasks retain editable notes alongside the explicit undo entry', async () => {
  const env=environment('task/p1/task1',{plan:async()=>undoPlan()});await env.controller.load();
  const html=renderWorkspace(env.controller.snapshot());
  assert.match(html,/data-workspace-action="undo-open"/);
  assert.match(html,/<textarea[^>]*name="note"/);
  assert.match(html,/old note/);
});

test('space projection invalidation keeps unknown undo journal across archive restore and drops old late reads', async () => {
  const storage=memory();let current=undoPlan(),status='active',failRead=false,release,delayed=false;const calls=[];
  const env=environment('task/p1/task1',{plan:async()=>{if(failRead)throw new Error('offline');return structuredClone(current);},
    space:async()=>{const frozen={...space,status};if(delayed)await new Promise(resolve=>{release=resolve;});return frozen;},
    undoTaskCompletion:async(p,t,body,options)=>{calls.push({body:structuredClone(body),key:options.key});throw new Error('lost');}}, {storage});
  await env.controller.load();await env.controller.action('undo-open');await env.controller.submit(values({operation:'undo_completion',reason:'original reason',confirm:'on'}));
  delayed=true;const old=env.controller.load(true);while(!release)await new Promise(resolve=>setImmediate(resolve));
  status='archived';env.controller.invalidateSpace('s1');delayed=false;failRead=true;await env.controller.load();
  release();await old;assert.equal(env.controller.snapshot().data,null);assert.match(env.controller.snapshot().error,/offline/);
  await env.controller.action('undo-open');assert.equal(calls.length,1);
  failRead=false;await env.controller.load();assert.equal(env.controller.snapshot().data.space.status,'archived');
  assert.match(renderWorkspace(env.controller.snapshot()),/重试原撤销/);
  status='active';env.controller.invalidateSpace('s1');await env.controller.load();
  await env.controller.submit(values({operation:'undo_completion',reason:'cannot replace original',confirm:'on'}));
  assert.deepEqual(calls[1],calls[0]);
});
