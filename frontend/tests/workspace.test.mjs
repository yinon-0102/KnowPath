import test from 'node:test';
import assert from 'node:assert/strict';
import { createWorkspaceController, updatedBindings, taskPayload, resolutionPayload, correctionPayload, dateRange } from '../src/workspace.js';
import * as workspaceModule from '../src/workspace.js';
import { renderWorkspace, workspaceTitles, correctionFields, updateWorkspaceConditions } from '../src/workspace-view.js';
import { createApi, ApiError } from '../src/api.js';

const memory = () => { const map = new Map(); return { getItem: key => map.get(key) || null, setItem: (key, value) => map.set(key, value) }; };
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
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url: new URL(url), options }); return new Response(JSON.stringify({ items: [], next_cursor: null })); });
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

test('management routes decode each identifier independently', () => {
  const path = workspaceModule.workspacePath('session', 'plan/a', '任务/b');
  assert.deepEqual(workspaceModule.workspaceRoute(path.slice('#/manage/'.length)), { page: 'session', id: 'plan/a', item: '任务/b' });
});
