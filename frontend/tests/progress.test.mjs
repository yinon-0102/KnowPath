import test from 'node:test';
import assert from 'node:assert/strict';

let component = {};
try { component = await import('../src/progress.js'); } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
const renderProgress = (...args) => { assert.equal(typeof component.renderProgress, 'function', 'renderProgress export must exist'); return component.renderProgress(...args); };
const renderLearningRecord = (...args) => { assert.equal(typeof component.renderLearningRecord, 'function', 'renderLearningRecord export must exist'); return component.renderLearningRecord(...args); };
const node = (id, status, sequence, extra = {}) => ({ node_id: 'node-' + id, plan_id: 'p1', task_id: id, sequence, title: '任务' + id, kind: 'learn', status, estimated_minutes: 20, topic_ids: [], active_session_id: null, historical: false, ...extra });
const progress = nodes => ({ space_id: 's1', rounds: [{ plan_id: 'p1', created_at: '2026-10-01T08:00:00Z', status: 'active', nodes }], completed_count: nodes.filter(row => row.status === 'completed').length, total_count: nodes.length, next_cursor: null });
const section = (html, view) => html.split(`data-progress-view="${view}"`)[1]?.split('</section>')[0] || '';
const record = (extra = {}) => ({ space_id: 's1', plan_id: 'p1', task_id: 't1', node_id: 'n1', task: { id: 't1', title: '线性变换', status: 'pending', estimated_minutes: 25 }, topics: [], sessions: [], assessments: [], total_elapsed_seconds: null, ...extra });

test('current view keeps copied nodes using current plan task ids and authoritative task states', () => {
  const data = { ...progress([]), rounds: [{ plan_id: 'old', created_at: '2026-09-01', status: 'superseded', nodes: [node('latest', 'pending', 8, { node_id: 'stable', plan_id: 'p1', historical: false })] }, ...progress([]).rounds] };
  const currentPlan = { id: 'p1', tasks: [{ id: 'latest', node_id: 'stable', sequence: 2, title: '当前副本', status: 'completed' }] };
  const html = renderProgress(data, { currentPlan, compact: true });
  assert.match(html, /当前计划/);
  assert.match(html, /data-plan="p1" data-task="latest"/);
  assert.match(html, /当前副本/);
  assert.match(html, /progress-node completed/);
  assert.doesNotMatch(html, /data-progress-view="cumulative"/);
});

test('nodes are ordered by sequence and only adjacent completed nodes color a connector', () => {
  const html = renderProgress(progress([node('c', 'pending', 3), node('b', 'completed', 2), node('a', 'completed', 1)]), { compact: true });
  assert.ok(html.indexOf('data-task="a"') < html.indexOf('data-task="b"'));
  assert.ok(html.indexOf('data-task="b"') < html.indexOf('data-task="c"'));
  assert.equal((html.match(/progress-connector completed/g) || []).length, 1);
  assert.equal((html.match(/progress-connector pending/g) || []).length, 1);
  assert.match(html, /aria-label="1\. 任务a，已完成/);
});

test('an active session adds a ring but never marks a pending task complete', () => {
  const html = renderProgress(progress([node('a', 'pending', 1, { active_session_id: 'session1' }), node('b', 'pending', 2)]), { compact: true });
  assert.match(html, /progress-node pending active/);
  assert.match(html, /学习中/);
  assert.doesNotMatch(html, /progress-node completed|progress-connector completed/);
  assert.match(html, /已完成 0 \/ 2/);
});

test('skipped and deferred statuses have explicit text without a completion glyph', () => {
  const html = renderProgress(progress([node('s', 'skipped', 1), node('d', 'deferred', 2)]), { compact: true });
  assert.match(html, /progress-node skipped/);
  assert.match(html, /progress-node deferred/);
  assert.match(html, /已跳过/);
  assert.match(html, /已延期/);
  assert.doesNotMatch(html, /progress-check/);
});

test('cumulative rounds use chronological order, preserve origin and collapse history', () => {
  const data = { space_id: 's1', rounds: [{ plan_id: 'p2', created_at: '2026-10-02', status: 'active', nodes: [node('new', 'pending', 1, { plan_id: 'p2' })] }, { plan_id: 'p1', created_at: '2026-10-01', status: 'superseded', nodes: [node('old', 'completed', 1)] }], completed_count: 1, total_count: 2, next_cursor: 'opaque"<cursor' };
  const html = renderProgress(data);
  const cumulative = section(html, 'cumulative');
  assert.ok(cumulative.indexOf('data-round="p1"') < cumulative.indexOf('data-round="p2"'));
  assert.match(cumulative, /<details class="progress-round" data-round="p1">/);
  assert.match(cumulative, /<details class="progress-round current" data-round="p2" open>/);
  assert.match(html, /data-action="progress-more"/);
  assert.match(html, /data-cursor="opaque&quot;&lt;cursor"/);
  assert.match(renderProgress(data, { expandedRounds: ['p1'] }), /data-round="p1" open>/);
});

test('progress escapes titles, status and identifiers and has an honest empty state', () => {
  const html = renderProgress(progress([node('" onclick="bad', '<img src=x onerror=bad>', 1, { title: '<script>bad</script>', node_id: '<svg>', plan_id: '" autofocus="' })]));
  assert.doesNotMatch(html, /<script>|<img|<svg>|data-plan=""/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&quot; onclick=&quot;bad/);
  assert.match(renderProgress({ rounds: [], completed_count: 0, total_count: 0 }), /暂无学习节点/);
  assert.doesNotMatch(renderProgress({ rounds: [] }), /已完成 \d/);
});

test('empty record renders no invented session, duration or association', () => {
  const html = renderLearningRecord(record());
  assert.match(html, /线性变换/);
  assert.match(html, /待学习/);
  assert.match(html, /预计 25 分钟/);
  assert.match(html, /未记录/);
  assert.match(html, /未关联/);
  assert.doesNotMatch(html, /0 分钟|data-action="task-assessment"/);
  assert.match(html, /href="#\/manage\/task\/p1\/t1"/);
});

test('session timestamps and elapsed totals identify paused time without inferring missing values', () => {
  const html = renderLearningRecord(record({ total_elapsed_seconds: 135, sessions: [{ session_id: 'sess1', plan_id: 'old', task_id: 'old-task', status: 'finished', started_at: '2026-10-01T08:00:00Z', finished_at: '2026-10-01T08:02:15Z', elapsed_seconds: 135, paused: false }, { session_id: 'sess2', plan_id: 'p1', task_id: 't1', status: 'active', started_at: '2026-10-02T08:00:00Z', finished_at: null, elapsed_seconds: null, paused: true }] }));
  assert.match(html, /2 分 15 秒/);
  assert.match(html, /包含暂停/);
  assert.match(html, /2026-10-01/);
  assert.match(html, /2026-10-02/);
  assert.match(html, /已暂停/);
  assert.match(html, /未记录/);
  assert.doesNotMatch(html, /专注时长|href="#\/manage\/session\/old/);
});

test('associated assessment results are explicit and use existing result links', () => {
  const html = renderLearningRecord(record({ assessments: [{ assessment_id: 'a/"1', kind: 'practice', status: 'completed', created_at: '2026-10-01', learning_session_id: 'sess1', result: { question_results: [{ score: 0.5, verdict: 'correct', feedback: '<script>result</script>' }], topic_results: [] } }, { assessment_id: 'a2', kind: 'diagnostic', status: 'generating', result: null }] }));
  assert.match(html, /练习测评/);
  assert.match(html, /href="#\/manage\/assessment-result\/a%2F%221"/);
  assert.match(html, /50%/);
  assert.match(html, /未出结果/);
  assert.doesNotMatch(html, /<script>/);
});

test('topics and frozen source references are escaped without unsafe source links', () => {
  const html = renderLearningRecord(record({ topics: [{ id: 'topic1', name: '<img src=x onerror=bad>', source_refs: [{ material_id: '<script>m</script>', material_version_id: 'version1', page: 3, section_path: ['<svg>', '第二章'], chunk_id: 'javascript:bad' }], source: 'current' }] }));
  assert.match(html, /&lt;img/);
  assert.match(html, /第 3 页/);
  assert.match(html, /version1/);
  assert.match(html, /&lt;svg&gt;/);
  assert.doesNotMatch(html, /<img|<script>|href="javascript:/);
});

test('only a current non-superseded record offers session and associated assessment execution', () => {
  const current = record({ current: true, sessions: [{ session_id: 'session1', plan_id: 'p1', task_id: 't1', status: 'active' }] });
  const html = renderLearningRecord(current);
  assert.match(html, /href="#\/manage\/session\/p1\/t1"/);
  assert.match(html, /data-action="task-assessment" data-plan="p1" data-task="t1" data-space="s1"[^>]*data-session="session1"/);
  for (const data of [record({ current: false }), record({ current: true, plan_status: 'superseded' }), record({ current: true, historical: true })]) {
    const historical = renderLearningRecord(data);
    assert.doesNotMatch(historical, /data-action="task-assessment"|href="#\/manage\/session\//);
    assert.match(historical, /查看任务记录/);
  }
});

test('busy and failure records expose accessible messages without modal markup', () => {
  assert.match(renderLearningRecord(null, { busy: true }), /role="status"[^>]*>.*正在加载学习记录/s);
  const error = renderLearningRecord(null, { error: '<script>network</script>' });
  assert.match(error, /role="alert"/);
  assert.match(error, /&lt;script&gt;network/);
  assert.doesNotMatch(error, /role="dialog"|<script>/);
  assert.match(renderLearningRecord(null), /暂无学习记录/);
});

test('current-node helper preserves carried session evidence without mutating either response', () => {
  assert.equal(typeof component.currentProgressNodes, 'function');
  const original = node('new-id', 'pending', 9, { node_id: 'stable', active_session_id: 's-old', historical: true });
  const data = { ...progress([]), rounds: [{ plan_id: 'origin', created_at: '2026-09-01', status: 'superseded', nodes: [original] }] };
  const currentPlan = { plan_id: 'new-plan', tasks: [{ task_id: 'current-id', node_id: 'stable', status: 'pending', title: '最新名称', sequence: 1 }] };
  const before = JSON.stringify([data, currentPlan]);
  assert.deepEqual(component.currentProgressNodes(data, currentPlan), [{ ...original, task_id: 'current-id', plan_id: 'new-plan', title: '最新名称', sequence: 1, historical: false }]);
  assert.equal(JSON.stringify([data, currentPlan]), before);
  assert.match(renderProgress(data, { currentPlan, compact: true }), /progress-node pending active/);
});

test('duration helper keeps zero distinct from missing and rounds seconds consistently', () => {
  assert.equal(typeof component.elapsedText, 'function');
  assert.equal(component.elapsedText(0), '0 秒');
  assert.equal(component.elapsedText(60), '1 分钟');
  assert.equal(component.elapsedText(90.7), '1 分 31 秒');
  for (const value of [null, undefined, '', -1, 'bad', Infinity]) assert.equal(component.elapsedText(value), '未记录');
});

test('ready plans enable execution and source fallback is labelled only when supplied', () => {
  const html = renderLearningRecord(record({ plan_status: 'ready', topics: [{ id: 'frozen', name: '封存主题', source: 'frozen', source_refs: [] }, { id: 'fallback', name: '补充主题', context_source: 'current', source_refs: [] }] }));
  assert.match(html, /data-action="task-assessment"/);
  assert.match(html, /会话封存资料/);
  assert.match(html, /当前资料补充/);
  assert.doesNotMatch(renderLearningRecord(record({ current: false, plan_status: 'ready' })), /data-action="task-assessment"/);
});

test('an active status without an associated active session does not add the session ring', () => {
  const html = renderProgress(progress([node('a', 'active', 1)]), { compact: true });
  assert.match(html, /progress-node pending/);
  assert.doesNotMatch(html, /class="progress-node[^\"]* active/);
});

test('assessment execution associates the latest matching finished session regardless of response order', () => {
  const html = renderLearningRecord(record({ current: true, sessions: [{ session_id: 'latest', plan_id: 'p1', task_id: 't1', status: 'finished', started_at: '2026-10-03T08:00:00Z' }, { session_id: 'other-task', plan_id: 'p1', task_id: 't2', status: 'finished', started_at: '2026-10-04T08:00:00Z' }, { session_id: 'earliest', plan_id: 'p1', task_id: 't1', status: 'finished', started_at: '2026-10-01T08:00:00Z' }] }));
  assert.match(html, /data-action="task-assessment"[^>]* data-session="latest"/);
});

test('exact backend topic provenance and escaped explicit source actions render without private fields', () => {
  const html = renderLearningRecord(record({ topics: [{ id: 's', name: '会话主题', source: 'session_snapshot', source_refs: [{ material_id: 'm"<1', material_version_id: 'v1', chunk_id: 'c1', page: 4, secret: 'private-secret' }] }, { id: 't', name: '任务主题', source: 'task_snapshot', fallback: true, source_refs: [] }] }));
  assert.match(html, /会话封存资料/);
  assert.match(html, /任务封存资料/);
  assert.match(html, /当前资料补充/);
  assert.match(html, /data-action="record-source" data-ref="\{&quot;material_id&quot;:&quot;m\\&quot;&lt;1&quot;/);
  assert.doesNotMatch(html, /private-secret|&quot;secret&quot;/);
});

test('completed current tasks can retest in a plan needing replan but cannot start a study session', () => {
  const html = renderLearningRecord(record({ current: true, plan_status: 'needs_replan', task: { id: 't1', title: '已学任务', status: 'completed' } }));
  assert.match(html, /data-action="task-assessment"/);
  assert.doesNotMatch(html, /href="#\/manage\/session\//);
  assert.doesNotMatch(renderLearningRecord(record({ current: true, plan_status: 'ready', task: { id: 't1', status: 'completed' } })), /href="#\/manage\/session\//);
});

test('only executable tasks in a ready current plan offer a new learning session', () => {
  assert.match(renderLearningRecord(record({ current: true, plan_status: 'ready' })), /href="#\/manage\/session\//);
  for (const status of ['skipped', 'deferred', 'completed']) assert.doesNotMatch(renderLearningRecord(record({ current: true, plan_status: 'ready', task: { id: 't1', status } })), /href="#\/manage\/session\//);
  assert.doesNotMatch(renderLearningRecord(record({ current: true, plan_status: 'needs_replan' })), /href="#\/manage\/session\//);
});

test('current plan omits carried historical tasks and keeps context identity and schedule order', () => {
  const plan = { id: 'p1', status: 'ready', tasks: [
    { id: 'old', status: 'completed', context: { historical: true, title: '过去完成' } },
    { id: 'second', status: 'pending', context: { node_id: 'stable-2', sequence: 1, position: 2, title: '当前第二项' } },
    { id: 'first', status: 'pending', context: { node_id: 'stable-1', sequence: 8, position: 1, title: '当前第一项' } },
  ] };
  const html = renderProgress(progress([]), { currentPlan: plan, compact: true });
  assert.doesNotMatch(html, /过去完成|data-task="old"/);
  assert.match(html, /已完成 0 \/ 2/);
  assert.match(html, /data-node="stable-1"/);
  assert.match(html, /当前第一项/);
  assert.ok(html.indexOf('data-task="first"') < html.indexOf('data-task="second"'));
});

test('task record uses sealed title and escaped learning notes', () => {
  const html = renderLearningRecord(record({ task: { id: 't1', status: 'completed', note: '<script>笔记</script>', context: { title: '封存任务名称' } } }));
  assert.match(html, /封存任务名称/);
  assert.match(html, /&lt;script&gt;笔记&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('pending tasks requiring replan and inherited history cannot start associated assessments', () => {
  assert.doesNotMatch(renderLearningRecord(record({ current: true, plan_status: 'needs_replan' })), /data-action="task-assessment"/);
  const historical = renderLearningRecord(record({ current: true, plan_status: 'ready', task: { id: 't1', status: 'completed', context: { historical: true } } }));
  assert.doesNotMatch(historical, /data-action="task-assessment"|href="#\/manage\/session\//);
  assert.match(historical, /历史记录/);
});

test('completed task assessment action explicitly requests retest', () => {
  const html = renderLearningRecord(record({ current: true, plan_status: 'needs_replan', task: { id: 't1', status: 'completed' } }));
  assert.match(html, /data-action="task-assessment"[^>]*data-kind="retest"/);
});


test('active session remains finishable when its plan needs replanning', () => {
  const html = renderLearningRecord(record({current: true, plan_status: 'needs_replan',
    sessions: [{session_id: 'active', plan_id: 'p1', task_id: 't1', status: 'active'}]}));
  assert.match(html, /继续学习会话/);
  assert.match(html, /href="#\/manage\/session\/p1\/t1"/);
  assert.doesNotMatch(html, /data-action="task-assessment"/);
});
