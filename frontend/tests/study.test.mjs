import test from 'node:test';
import assert from 'node:assert/strict';
let study = {};
try { study = await import('../src/study.js'); } catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
const space = { id: 's', status: 'active', profile_version: 1, bindings: [{ material_id: 'm', material_version_id: 'v' }] };
const ref = { material_id: 'm', material_version_id: 'v', chunk_id: 'c', page: 3 };
const plan = { id: 'p', space_id: 's', status: 'ready', version: 1, tasks: [
  { id: 'a', node_id: 'na', title: '变量', status: 'pending', topic_ids: ['ta'], context: { topics: [{ id: 'ta', name: '变量', source_refs: [ref] }] } },
  { id: 'b', node_id: 'nb', title: '函数', status: 'pending', topic_ids: ['tb'], context: { topics: [{ id: 'tb', name: '函数', source_refs: [ref] }] } },
] };
const memory = () => { const values = new Map(); return { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; };

test('PDF learning boundaries use frozen topic names and all referenced pages', () => {
 const html=study.renderStudy({space,plan,task:plan.tasks[0],topics:[{name:'第一章 总则 · 第一—六条'}],refs:[{...ref,page:1},{...ref,page:2}],lessons:[]});
 assert.match(html,/本节点学习范围/);assert.match(html,/第一章 总则 · 第一—六条/);assert.match(html,/第 1—2 页/);
});
test('study preserves source grounded objectives and bounded scope in both reading modes', () => {
  for (const mode of ['original', 'explanation']) {
    const html = study.renderStudy({space,plan,task:plan.tasks[0],mode,topics:[{name:'条件概率',content_role:'introduction',scope_summary:'给定事件后限制样本空间。',learning_objectives:['检查条件事件的概率非零。','说明 <事件> 的范围。'],extraction:{review_required:true}}],refs:[],lessons:[]});
    assert.match(html,/给定事件后限制样本空间。/);
    assert.match(html,/检查条件事件的概率非零。/);
    assert.match(html,/说明 &lt;事件&gt; 的范围。/);
    assert.match(html,/章节导言/);
    assert.match(html,/需要核对来源/);
  }
});
test('study identifies instructional and review roles while legacy topics stay compatible', () => {
  for (const [content_role, label] of [['instruction','学习单元'],['review','复习单元']]) {
    const html=study.renderStudy({space,plan,task:plan.tasks[0],topics:[{name:'边界',content_role,scope_summary:'检查观测条件。'}],refs:[],lessons:[]});
    assert.ok(html.includes(label));
  }
  const legacy=study.renderStudy({space,plan,task:plan.tasks[0],topics:[{name:'旧主题'}],refs:[],lessons:[]});
  assert.ok(!legacy.includes('study-unit-boundary'));
});
test('review required units remain readable and save sessions without offering blocked assessment', () => {
  for (const session of [undefined,{id:'session',task_id:'a',status:'active'}]) {
    const html=study.renderStudy({space,plan,task:plan.tasks[0],session,topics:[{name:'图表',is_assessable:false,automatic_questions:false,extraction:{review_required:true},source_refs:[ref]}],refs:[ref],lessons:[]});
    assert.ok(!html.includes('data-study-action="assess"'));
    assert.ok(!html.includes('data-study-action="finish-assess"'));
    assert.match(html,/需核对来源/);
    assert.match(html,/可继续阅读/);
    assert.match(html,/上传修正版本/);
    assert.match(html,/记录任务完成情况/);
    if(session) assert.match(html,/data-study-action="finish"/);
  }
});
const api = overrides => ({ learningRecord: async (_, task) => ({ space_id: 's', task: plan.tasks.find(t => t.id === task), topics: plan.tasks.find(t => t.id === task).context.topics, sessions: [] }),
  studyLessons: async () => ({ items: [] }), source: async () => ({ text: '真实原文', page: 3 }), materialFile: async () => new Response('%PDF-1.7', { headers: { 'Content-Type': 'application/pdf' } }), ...overrides });

test('explanation automatically requests the current adaptation while retaining a readable old lesson', async () => {
  let generated = 0;
  const old = { id: 'old', status: 'completed', style: 'plain', instructions: '', adaptation_fingerprint: 'old' };
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [old], adaptation_fingerprint: 'new', adaptation_context: { basis: 'new' } }),
    studyLesson: async () => old, generateStudyLesson: async () => { generated++; throw new Error('模型离线'); } }), storage: memory() });
  await c.load({ space, plan }); await c.setMode('explanation');
  assert.equal(generated, 1); assert.equal(c.snapshot().lesson.id, 'old'); assert.match(c.snapshot().lessonError, /离线/); c.dispose();
});

test('explicit historical lesson remains selected when personalization basis changes', async () => {
  let generated = 0;
  const old = { id: 'old', status: 'completed', style: 'plain', instructions: '', adaptation_fingerprint: 'old' };
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [old], adaptation_fingerprint: 'new' }), studyLesson: async () => old,
    generateStudyLesson: async () => { generated++; } }), storage: memory() });
  await c.load({ space, plan }); await c.readLesson('old'); await c.setMode('explanation'); assert.equal(generated, 0); c.dispose();
});

test('study module exposes a controller and real-data view', () => {
  assert.equal(typeof study.createStudyController, 'function'); assert.equal(typeof study.renderStudy, 'function');
});
test('study shows only confirmed active time separately from paused duration', () => {
  const html = study.renderStudy({ space, plan, task: plan.tasks[0], refs: [], lessons: [], session: { status: 'active', task_id: 'a', activity: { active_seconds: 120, paused_seconds: 60, reliable: true } } });
  assert.match(html, /服务端确认学习时间/); assert.match(html, /暂停时间/);
});
test('opening a node is preview and never starts a learning session', async () => {
  let starts = 0;
  const c = study.createStudyController({ api: api({ startSession: async () => { starts++; } }), storage: memory() });
  await c.load({ space, plan });
  assert.equal(c.snapshot().task.id, 'a'); assert.equal(c.snapshot().mode, 'original'); assert.equal(starts, 0);
});

test('generic continuation ignores a saved completed node but explicit history remains readable', async () => {
  const storage = memory(); storage.setItem('knowpath-study-selection:s', JSON.stringify('na'));
  const updated = { ...plan, tasks: [{ ...plan.tasks[0], status: 'completed' }, plan.tasks[1]] };
  const c = study.createStudyController({ api: api(), storage });
  await c.load({ space, plan: updated }); assert.equal(c.snapshot().task.id, 'b');
  await c.load({ space, plan: updated, taskId: 'a' }); assert.equal(c.snapshot().task.id, 'a'); c.dispose();
});

test('processed rounds show summary instead of reopening the first completed node', async () => {
  const c = study.createStudyController({ api: api(), storage: memory() });
  const completed = { ...plan, tasks: plan.tasks.map(task => ({ ...task, status: 'completed' })) };
  await c.load({ space, plan: completed });
  assert.equal(c.snapshot().task, undefined);
  assert.match(study.renderStudy(c.snapshot()), /本轮总结/); c.dispose();
});
test('active session chooses its node and prevents starting a second node', async () => {
  let starts = 0;
  const c = study.createStudyController({ api: api({ startSession: async () => { starts++; } }), storage: memory() });
  const activeSession = { session_id: 'session', task_id: 'b', plan_id: 'p', status: 'active' };
  await c.load({ space, plan, activeSession }); assert.equal(c.snapshot().task.id, 'b');
  await c.load({ space, plan, activeSession, taskId: 'a' });
  await assert.rejects(c.start(), /会话/); assert.equal(starts, 0);
});
test('late node responses cannot replace the new selection', async () => {
  let resolve;
  const c = study.createStudyController({ api: api({ learningRecord: (_, task) => task === 'a' ? new Promise(done => { resolve = done; }) : Promise.resolve({ space_id: 's', topics: plan.tasks[1].context.topics }) }), storage: memory() });
  const first = c.load({ space, plan, taskId: 'a' });
  await c.load({ space, plan, taskId: 'b' }); resolve({ space_id: 's', topics: [{ name: '错误旧节点' }] }); await first;
  assert.equal(c.snapshot().task.id, 'b'); assert.equal(c.snapshot().topics[0].name, '函数');
});
test('original reader checks source and fetches exact frozen version rather than latest material', async () => {
  const calls = [];
  const c = study.createStudyController({ api: api({ source: async (r, id) => { calls.push(['source', r.material_version_id, id]); return { text: '真实', page: 3 }; }, materialFile: async (m, v, s) => { calls.push(['file', m, v, s]); return new Response('%PDF', { headers: { 'Content-Type': 'application/pdf' } }); } }), storage: memory() });
  await c.load({ space, plan }); await c.readSource(0);
  assert.deepEqual(calls, [['source', 'v', 's'], ['file', 'm', 'v', 's']]);
  assert.equal(c.snapshot().reader.kind, 'pdf'); c.dispose();
});
test('source audit failure never reveals a retained document', async () => {
  let denied = false;
  const c = study.createStudyController({ api: api({ source: async () => { if (denied) throw new Error('来源已删除'); return { text: '真实' }; } }), storage: memory() });
  await c.load({ space, plan }); await c.readSource(0); denied = true;
  await assert.rejects(c.readSource(0), /删除/); assert.equal(c.snapshot().reader, null); c.dispose();
});
test('switching to explanation does not call the model and reaudits saved body', async () => {
  let reads = 0, generates = 0;
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [{ id: 'l', status: 'completed', style: 'plain', instructions: '' }] }), studyLesson: async () => { reads++; return { id: 'l', status: 'completed', sections: [] }; }, generateStudyLesson: async () => { generates++; } }), storage: memory() });
  await c.load({ space, plan }); await c.setMode('explanation'); await c.setMode('original'); await c.setMode('explanation');
  assert.equal(generates, 0); assert.equal(reads, 2);
});
test('explicit generation preserves task linkage and a stable retry identity', async () => {
  const calls = [];
  const c = study.createStudyController({ api: api({ generateStudyLesson: async (s, n, body, opts) => { calls.push({ s, n, body, key: opts.key }); throw new Error('网络断开'); } }), storage: memory() });
  await c.load({ space, plan }); await assert.rejects(c.generate(), /网络/); await assert.rejects(c.generate(), /网络/);
  assert.equal(calls[0].n, 'na'); assert.equal(calls[0].body.task_id, 'a'); assert.equal(calls[0].key, calls[1].key);
});
test('finishing saves session but does not mark task mastered or completed', async () => {
  let finishes = 0, updates = 0;
  const c = study.createStudyController({ api: api({ finishSession: async () => { finishes++; return { status: 'finished', session_id: 'x', elapsed_seconds: 10 }; }, updateTask: async () => { updates++; } }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await c.finish(); assert.equal(finishes, 1); assert.equal(updates, 0); assert.equal(c.snapshot().task.status, 'pending');
});
for (const code of ['TIMEOUT', 'NETWORK']) test(`finishing confirms the same session after ${code} without finishing a different node`, async () => {
  const calls = [];
  const c = study.createStudyController({ api: api({ finishSession: async id => {
    calls.push(id);
    if (calls.length === 1) throw Object.assign(new Error('result unknown'), { code });
    return { status: 'finished', session_id: id, elapsed_seconds: 10 };
  } }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await c.finish();
  assert.deepEqual(calls, ['x', 'x']);
  assert.equal(c.snapshot().session.status, 'finished');
  assert.equal(c.snapshot().activeSession, null);
  assert.equal(c.snapshot().task.status, 'pending');
});
test('finish confirmation never retries a business rejection', async () => {
  let calls = 0;
  const c = study.createStudyController({ api: api({ finishSession: async () => {
    calls++; throw Object.assign(new Error('archived'), { code: 'SPACE_ARCHIVED' });
  } }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await assert.rejects(c.finish(), /archived/);assert.equal(calls, 1);
});
test('finish confirmation rejects a different session and preserves unconfirmed state', async () => {
  const c = study.createStudyController({ api: api({ finishSession: async () => ({ status: 'finished', session_id: 'other' }) }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await assert.rejects(c.finish(), /尚未确认/); assert.equal(c.snapshot().session.status, 'active'); c.dispose();
});
test('a late finish confirmation cannot overwrite a newly selected node session', async () => {
  let resolve;
  const c = study.createStudyController({ api: api({ finishSession: () => new Promise(done => { resolve = done; }) }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  const pending = c.finish();
  await c.load({ space, plan, taskId: 'b', activeSession: { session_id: 'y', task_id: 'b', plan_id: 'p', status: 'active' } });
  resolve({ status: 'finished', session_id: 'x' }); await pending;
  assert.equal(c.snapshot().session.session_id, 'y'); assert.equal(c.snapshot().session.status, 'active'); c.dispose();
});
test('two ambiguous finish failures do not claim the session was saved', async () => {
  let calls = 0;
  const c = study.createStudyController({ api: api({ finishSession: async () => { calls++; throw Object.assign(new Error('result unknown'), { code: 'TIMEOUT' }); } }), storage: memory() });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await assert.rejects(c.finish(), /result unknown/); assert.equal(calls, 2); assert.equal(c.snapshot().session.status, 'active'); c.dispose();
});
test('refresh failure after a confirmed finish does not repeat the finish command', async () => {
  let calls = 0;
  const c = study.createStudyController({ api: api({ finishSession: async id => { calls++; return { status: 'finished', session_id: id }; } }), storage: memory(), committed: async () => { throw Object.assign(new Error('refresh failed'), { code: 'TIMEOUT' }); } });
  await c.load({ space, plan, activeSession: { session_id: 'x', task_id: 'a', plan_id: 'p', status: 'active' } });
  await assert.rejects(c.finish(), /refresh failed/); assert.equal(calls, 1); assert.equal(c.snapshot().session.status, 'finished'); c.dispose();
});
test('study view escapes titles, safe Markdown and exposes actual empty states', () => {
  const html = study.renderStudy({ space, plan, task: { ...plan.tasks[0], title: '<script>x</script>' }, topics: [], refs: [], mode: 'explanation', lessons: [], style: 'plain', instructions: '', messages: [] });
  assert.ok(html.includes('&lt;script&gt;')); assert.ok(html.includes('生成讲解')); assert.ok(!html.includes('<script>'));
  assert.ok(study.renderStudy({ space, plan: null }).includes('生成学习计划'));
});
test('explicit page selection changes the PDF render identity and persists', async () => {
  const storage = memory();
  const c = study.createStudyController({ api: api(), storage }); await c.load({ space, plan }); await c.readSource(0);
  const first = c.snapshot().reader.token; c.page(8);
  assert.equal(c.snapshot().reader.page, 8); assert.notEqual(c.snapshot().reader.token, first);
  c.dispose(); await c.load({ space, plan }); await c.readSource(0); assert.equal(c.snapshot().reader.page, 8); c.dispose();
});
test('restored preferences returned by lesson index select the matching saved lecture', async () => {
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [], preferences: { lesson_style: 'deep', lesson_instructions: '保留术语' } }) }), storage: memory() });
  await c.load({ space, plan }); assert.equal(c.snapshot().style, 'deep'); assert.equal(c.snapshot().instructions, '保留术语');
});
test('missing raw file offers explicitly labelled audited parsed text as another reading choice', async () => {
  const c = study.createStudyController({ api: api({ materialFile: async () => { throw new Error('原始资料不存在'); } }), storage: memory() });
  await c.load({ space, plan }); await assert.rejects(c.readSource(0), /不存在/);
  assert.match(study.renderStudyReader(c.snapshot()), /解析文本/);
  await c.readParsedSource(0); assert.equal(c.snapshot().reader.kind, 'parsed'); assert.equal(c.snapshot().reader.text, '真实原文');
});
test('cached original stays hidden while a new source audit is pending', async () => {
  let resolve, delay = false;
  const c = study.createStudyController({ api: api({ source: () => delay ? new Promise(done => { resolve = done; }) : Promise.resolve({ text: '真实原文', page: 3 }) }), storage: memory() });
  await c.load({ space, plan }); await c.readSource(0); await c.setMode('explanation'); delay = true;
  const pending = c.setMode('original');
  const html = study.renderStudyReader(c.snapshot());
  assert.match(html, /data-study-document[^>]*hidden/); assert.doesNotMatch(html, /<a[^>]+data-study-open-original/);
  resolve({ text: '真实原文', page: 3 }); await pending; c.dispose();
});
test('a late lecture version read cannot replace a newer user choice', async () => {
  const pending = new Map();
  const c = study.createStudyController({ api: api({ studyLesson: id => new Promise(done => pending.set(id, done)) }), storage: memory() });
  await c.load({ space, plan }); const first = c.readLesson('old'), latest = c.readLesson('new');
  pending.get('new')({ id: 'new', status: 'completed', sections: [] }); await latest;
  pending.get('old')({ id: 'old', status: 'completed', sections: [] }); await first;
  assert.equal(c.snapshot().lesson.id, 'new');
});
test('starting before a first question does not invent a conversation on reload', async () => {
  let history = 0; const storage = memory();
  const c = study.createStudyController({ api: api({ startSession: async () => ({ session_id: 'started', task_id: 'a', plan_id: 'p', status: 'active' }), studyMessages: async () => { history++; throw new Error('会话不存在'); } }), storage });
  await c.load({ space, plan }); await c.start(); c.dispose(); await c.load({ space, plan });
  assert.equal(history, 0); assert.equal(c.snapshot().error, '');
});
test('a late preference save cannot mutate another selected node', async () => {
  let resolve;
  const c = study.createStudyController({ api: api({ updateProfile: () => new Promise(done => { resolve = done; }) }), storage: memory() });
  await c.load({ space, plan }); const save = c.preferences('deep', '只属于A'); await c.load({ space, plan, taskId: 'b' });
  resolve({ profile: { preferences: { value: { lesson_style: 'deep', lesson_instructions: '只属于A' } } }, profile_version: 2 }); await save;
  assert.equal(c.snapshot().task.id, 'b'); assert.equal(c.snapshot().instructions, '');
});
test('ambiguous regenerate retains its idempotency key across a reload', async () => {
  const keys = [], storage = memory(), client = api({ generateStudyLesson: async (_, __, body, options) => { keys.push(options.key); throw new Error('断线'); } });
  const first = study.createStudyController({ api: client, storage }); await first.load({ space, plan }); await assert.rejects(first.generate(true)); first.dispose();
  const restored = study.createStudyController({ api: client, storage }); await restored.load({ space, plan }); await assert.rejects(restored.generate(true)); assert.equal(keys[0], keys[1]);
});
test('node notebook action opens the linked chapter and stale plan offers replanning', () => {
  const html = study.renderStudy({ space, plan: { ...plan, status: 'needs_replan' }, task: plan.tasks[0], refs: [], mode: 'original', record: { note: { chapter_id: 'chapter' } } });
  assert.match(html, /notes\?chapter=chapter/); assert.match(html, /重新规划/);
});

test('selected saved lecture version survives a reload and mode switches', async () => {
  const storage = memory(), versions = [1, 2].map(version => ({ id: `l${version}`, version, status: 'completed', style: 'plain', instructions: '' }));
  const client = api({ studyLessons: async () => ({ items: versions }), studyLesson: async id => ({ ...versions.find(v => v.id === id), sections: [] }) });
  const c = study.createStudyController({ api: client, storage });
  await c.load({ space, plan }); await c.setMode('explanation'); await c.readLesson('l1');
  await c.setMode('original'); await c.setMode('explanation'); assert.equal(c.snapshot().lesson.id, 'l1');
  c.dispose(); await c.load({ space, plan }); assert.equal(c.snapshot().lesson.id, 'l1'); c.dispose();
});

test('paged follow-up history stays chronological without duplicate messages', async () => {
  const c = study.createStudyController({ api: api({ studyMessages: async (_, __, params) => params?.cursor
    ? { items: [{ id: 'm1', sequence: 1 }, { id: 'm2', sequence: 2 }], next_cursor: null }
    : { items: [{ id: 'm2', sequence: 2 }, { id: 'm3', sequence: 3 }], next_cursor: 'earlier' } }), storage: memory() });
  await c.load({ space, plan }); c.snapshot().chat.chatSession = 'chat'; await c.loadHistory(); await c.moreHistory();
  assert.deepEqual(c.snapshot().messages.map(m => m.id), ['m1', 'm2', 'm3']); c.dispose();
});

test('late original text decoding cannot overwrite another node reader', async () => {
  let resolve;
  const c = study.createStudyController({ api: api({ materialFile: async () => ({ blob: async () => ({ type: 'text/plain', text: () => new Promise(done => { resolve = done; }) }) }) }), storage: memory() });
  await c.load({ space, plan }); const pending = c.readSource(0);
  while (!resolve) await new Promise(done => setTimeout(done, 0));
  await c.load({ space, plan, taskId: 'b' }); resolve('旧节点原文'); await pending;
  assert.equal(c.snapshot().task.id, 'b'); assert.ok(c.snapshot().reader == null); c.dispose();
});

test('raw reader replaces parsed fallback after the original file is available', async () => {
  const c = study.createStudyController({ api: api(), storage: memory() });
  await c.load({ space, plan }); await c.readParsedSource(0); assert.equal(c.snapshot().reader.kind, 'parsed');
  await c.readSource(0); assert.equal(c.snapshot().reader.kind, 'pdf'); c.dispose();
});

test('background lecture polling cannot override a pending version selection', async () => {
  const pending = new Map(), versions = [1, 2].map(version => ({ id: `l${version}`, version, status: 'completed', style: 'plain', instructions: '' }));
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: versions }), studyLesson: id => new Promise(done => pending.set(id, done)) }), storage: memory() });
  await c.load({ space, plan }); c.snapshot().mode = 'explanation'; const read = c.readLesson('l1');
  const poll = c.refreshLessons(); await new Promise(done => setTimeout(done, 0));
  if (pending.has('l2')) pending.get('l2')({ ...versions[1], sections: [] });
  pending.get('l1')({ ...versions[0], sections: [] }); await Promise.all([read, poll]);
  assert.equal(c.snapshot().lesson.id, 'l1'); c.dispose();
});

test('Markdown original is formatted by default and can show audited source text', async () => {
  const c = study.createStudyController({ api: api({ materialFile: async () => new Response('# Python 原文\n\n```python\nx = 1\n```', { headers: { 'Content-Type': 'text/markdown' } }) }), storage: memory() });
  await c.load({ space, plan }); await c.readSource(0);
  assert.equal(c.snapshot().reader.kind, 'markdown');
  assert.match(study.renderStudyReader(c.snapshot()), /<h1>Python 原文<\/h1>/);
  c.readerFormat('source'); assert.match(study.renderStudyReader(c.snapshot()), /# Python 原文/);
  assert.doesNotMatch(study.renderStudyReader(c.snapshot()), /<h1>Python 原文/); c.dispose();
});

test('Markdown filename identifies original even when server sends plain text', async () => {
  const c = study.createStudyController({ api: api({ materialFile: async () => new Response('# 标题', { headers: { 'Content-Type': 'text/plain', 'Content-Disposition': "inline; filename*=UTF-8''notes.md" } }) }), storage: memory() });
  await c.load({ space, plan }); await c.readSource(0); assert.equal(c.snapshot().reader.kind, 'markdown'); c.dispose();
});

test('lecture stream exposes cumulative sections before completion without enabling follow-up', async () => {
  let callbacks, finish, complete = false;
  const item = { id: 'l', run_id: 'run', style: 'plain', instructions: '', status: 'generating' };
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [{ ...item, status: complete ? 'completed' : 'generating' }] }),
    readLessonRun: (_, options) => { callbacks = options; return new Promise(resolve => { finish = resolve; }); },
    studyLesson: async () => ({ id: 'l', status: 'completed', sections: [{ kind: 'core', markdown: '最终正文' }] }) }), storage: memory(), delay: 60000 });
  await c.load({ space, plan }); await c.setMode('explanation');
  callbacks.onEvent('lesson.section', { lesson_id: 'l', sections: [{ kind: 'core', markdown: '**增量正文**' }], citations: [] });
  assert.match(study.renderStudy(c.snapshot()), /<strong>增量正文<\/strong>/);
  assert.equal(c.snapshot().lesson, null); await assert.rejects(c.ask('问题'), /有效讲义/);
  callbacks.onEvent('lesson.reset', { lesson_id: 'l' }); assert.equal(c.snapshot().lessonDraft, null);
  complete = true; finish({}); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(c.snapshot().lesson.id, 'l'); assert.equal(c.snapshot().lessonDraft, null); c.dispose();
});

test('late lecture sections cannot cross node selection or mode changes', async () => {
  let callbacks;
  const c = study.createStudyController({ api: api({ studyLessons: async () => ({ items: [{ id: 'l', run_id: 'run', status: 'generating', style: 'plain', instructions: '' }] }),
    readLessonRun: (_, options) => { callbacks = options; return new Promise(() => {}); } }), storage: memory(), delay: 60000 });
  await c.load({ space, plan }); await c.setMode('explanation'); const old = callbacks;
  await c.load({ space, plan, taskId: 'b' }); old.onEvent('lesson.section', { lesson_id: 'l', sections: [{ markdown: '旧节点内容' }] });
  assert.ok(!c.snapshot().lessonDraft); assert.equal(old.signal.aborted, true);
  await c.setMode('explanation'); await c.setMode('original'); callbacks.onEvent('lesson.section', { lesson_id: 'l', sections: [{ markdown: '隐藏内容' }] });
  assert.ok(!c.snapshot().lessonDraft); c.dispose();
});

test('follow-up renders streaming text then removes draft on failure', async () => {
  let options, reject;
  const c = study.createStudyController({ api: api({ sendStudyMessage: async () => ({ run_id: 'r', session_id: 'chat' }),
    readMessage: (_, value) => { options = value; return new Promise((_, no) => { reject = no; }); } }), storage: memory() });
  await c.load({ space, plan }); c.snapshot().lesson = { id: 'l', status: 'completed' };
  const pending = c.ask('为什么'); await new Promise(resolve => setTimeout(resolve, 0));
  options.onDelta('部分', { text: '**部分答案**', provisional: true });
  assert.match(study.renderStudy(c.snapshot()), /<strong>部分答案<\/strong>/);
  reject(Object.assign(new Error('生成失败'), { status: 400, code: 'RUN_FAILED' })); await assert.rejects(pending);
  assert.equal(c.snapshot().chatDraft, null); assert.equal(c.snapshot().pendingChat, null); c.dispose();
});

test('stopping while question submission is pending cancels the acknowledged run', async () => {
  let resolve, cancelled;
  const c = study.createStudyController({ api: api({ sendStudyMessage: () => new Promise(done => { resolve = done; }),
    cancelRun: async id => { cancelled = id; }, readMessage: async () => { throw new Error('不应开始接收'); } }), storage: memory() });
  await c.load({ space, plan }); c.snapshot().lesson = { id: 'l', status: 'completed' };
  const ask = c.ask('问题'); await c.cancelChat(); resolve({ run_id: 'r', session_id: 'chat' });
  await assert.rejects(ask, error => error.name === 'AbortError'); assert.equal(cancelled, 'r'); assert.equal(c.snapshot().pendingChat, null); c.dispose();
});

test('completed study node exposes task undo management and keeps history read only', () => {
  const task = {...plan.tasks[0],status:'completed'};
  const html=study.renderStudy({space,plan,task,topics:[],refs:[],lessons:[]});
  assert.match(html,/撤销本次任务完成/); assert.match(html,/#\/manage\/task\/p\/a/);
  const history=study.renderStudy({space,plan,task,current:false,topics:[],refs:[],lessons:[]});
  assert.doesNotMatch(history,/撤销本次任务完成/);
});
