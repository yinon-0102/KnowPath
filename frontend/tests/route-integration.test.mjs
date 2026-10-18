import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { nodeSessionState, renderProgress, currentProgressNodes, taskDisplay } from '../src/progress.js';
import { nextLearningStep, spacePath, parseRoute, createTaskCompletionController, createWorkbenchController } from '../src/workbench.js';
import { studyPath, renderStudy, createStudyController } from '../src/study.js';
import { createWorkspaceController } from '../src/workspace.js';
import { assessmentTaskCompletion } from '../src/learning.js';
import { esc, storageRead, storageWrite, mergeSpaceList, mergeSpaceProjection } from '../src/store.js';
const app=await readFile(new URL('../src/app.js',import.meta.url),'utf8');
const extract=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));

for (const entry of ['#/learning/diagnostic', '#/learning']) test(`initial render before backend data preserves selected-space legacy recovery: ${entry}`, async () => {
 const selected = { id: 'selected-space' }, storage = new Map([['knowpath-selected-live', JSON.stringify(selected.id)]]);
 const session = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
 assert.equal(storageRead(session, 'knowpath-selected-live', ''), selected.id);
 const state = { selected: '', loading: true, error: '', live: { spaces: [], materials: [] }, dataRefreshId: 0, topics: new Map([[selected.id, []]]) };
 const location = { hash: entry, replace(hash) { this.hash = hash; } };
 let release; const spaces = new Promise(resolve => { release = resolve; });
 const api = { spaces: async () => spaces, materials: async () => [], space: async () => selected, topics: async () => [],
   recoverableAssessments: async () => ({ items: [{ id: 'a', space_id: selected.id }] }),
   assessment: async id => ({ id, space_id: selected.id, status: 'in_progress', question_count: 5,
     questions: Array.from({ length: 5 }, (_, i) => ({ id: `q${i + 1}` })), answers: Array.from({ length: 4 }, (_, i) => ({ question_id: `q${i + 1}` })) }),
 };
 const { createLearningController } = await import('../src/learning.js');
 const currentSpace = () => state.live.spaces.find(space => space.id === state.selected) || state.live.spaces[0];
 const route = () => parseRoute(location.hash);
 const learning = createLearningController({ api, storage: session,
   context: () => ({ spaceId: currentSpace()?.id || '', active: true, routeKey: location.hash, setupReady: state.assessmentReadyRoute === location.hash }) });
 const noop = () => '', context = vm.createContext({ state, location, session, token: 'fixture-token', api, learning, currentSpace, route, parseRoute, storageRead, storageWrite, mergeSpaceList,
   data: () => state.live, byId: id => state.live.spaces.find(space => space.id === id), spacePath, routeState: () => ({ tab: 'assessment', learningSection: 'diagnostic' }),
   workbench: { load: async () => {}, invalidate() {} }, study: { dispose() {}, snapshot: () => ({}) }, notes: { dispose() {}, snapshot: () => ({}) },
   $: () => null, main: {}, document: {}, assessmentAnswerFocus: null, renderVersion: 0, routes: [],
   captureAssessmentAnswer: noop, header: noop, legacyMaterialRedirect: noop, captureStudySidebarScroll: noop, subbar: noop, restoreAssessmentAnswer: noop,
   syncAssessmentTicker: noop, updateProgressPosition: noop, updateStudySidebar: noop, updateStudyTabs: noop, scheduleMaterialRefresh: noop,
   pageHeading: noop, overview: noop, spacesPage: noop, materialsPage: noop, graphPage: noop, planPage: noop, assistantPage: noop, spacePage: noop,
   managedPage: noop, renderNotes: noop, renderLearning: noop, spaceSelect: noop,
 });
 vm.runInContext(extract('function legacySpaceRedirect(', '\nfunction updateAssessmentActivity'), context);
 vm.runInContext(extract('function render()', '\nconst pendingMaterialStatuses'), context);
 vm.runInContext(extract('async function connectBackend(', '\nfunction newSpaceModal'), context);
 vm.runInContext(app.slice(app.lastIndexOf("state.selected = storageRead(session, 'knowpath-selected-live'")), context);
 const beforeBackend = location.hash;
 release([{ id: 'first-space' }, selected]); await state.connecting;
 assert.equal(beforeBackend, entry, 'initial render must not erase the legacy route before spaces are loaded');
 assert.equal(location.hash, '#/space/selected-space/assessment');
 assert.equal(learning.snapshot().assessmentId, 'a'); assert.equal(learning.snapshot().diagnostic.current_question.id, 'q5');
 assert.equal(learning.snapshot().setupReady, true);
});

for (const planStatus of ['ready', 'superseded', 'needs_replan', 'route-changed']) test(`a recovered task round validates its route owner only when starting a new round: ${planStatus}`, async () => {
 const space = { id: 's1', bindings: [], topic_ids: ['topic', 'other'] }, location = { hash: '#/space/s1/assessment?plan=p&task=t&session=ss' };
 const state = { loading: false, error: '', spaceTab: 'assessment', learningSection: 'diagnostic', topics: new Map([['s1', []]]) };
 const round = { id: 'a', space_id: 's1', kind: 'retest', status: 'in_progress', plan_id: 'p', task_id: 't', learning_session_id: 'ss',
   feedback_policy: 'after_round', question_count: 1, questions: [{ id: 'q', type: 'single_choice', options: [{ id: 'A', text: 'A' }] }], answers: [{ question_id: 'q' }] };
 const creates = []; let planReads = 0, releasePlan;
 const api = { recoverableAssessments: async () => ({ items: [{ id: 'a', space_id: 's1', plan_id: 'p', task_id: 't' }] }),
   assessment: async () => structuredClone(round), space: async () => space, topics: async () => space.topic_ids.map(id => ({ id, source_refs: [{}] })),
   plan: async () => { planReads++; if (planStatus === 'route-changed') await new Promise(resolve => { releasePlan = resolve; });
     return { id: 'p', space_id: 's1', status: planStatus === 'route-changed' ? 'ready' : planStatus, tasks: [{ id: 't', status: 'pending', topic_ids: ['topic'] }] }; },
   finalizeAssessment: async () => { round.status = 'completed'; return { run_id: 'done' }; }, assessmentResult: async () => ({ assessment_id: 'a', question_results: [] }),
   waitRun: async () => {}, createAssessment: async (_, body) => { creates.push(body); throw new Error('captured'); } };
 const { createLearningController, renderLearning } = await import('../src/learning.js');
 const { taskAssessmentContext } = await import('../src/workbench.js');
 const stored = new Map(), session = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) };
 const context = vm.createContext({ api, state, location, session, createLearningController, taskAssessmentContext,
   currentSpace: () => space, route: () => parseRoute(location.hash), isLearningRoute: () => true,
   updateAssessmentActivity() {}, render() {}, workbench: { load: async () => {}, invalidate() {} }, study: { dispose() {} }, notes: { dispose() {} } });
 if (app.includes('async function prepareAssessmentCreation(')) vm.runInContext(extract('async function prepareAssessmentCreation(', '\nasync function loadRoute()'), context);
 vm.runInContext(extract('const learning = createLearningController(', '\nconst workspace ='), context);
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 const learning = vm.runInContext('learning', context);
 await context.loadRoute(); await learning.finalize();
 assert.equal(planReads, 0, 'historical round recovery and completion must ignore new-creation plan restrictions');
 assert.equal(learning.snapshot().assessmentStatus, 'completed');
 assert.match(renderLearning({ space, state: learning.snapshot() }), /id="learning-start-form"/);
 const starting = learning.start({ kind: 'retest', count: '6', type: 'short_answer' });
 if (planStatus === 'route-changed') {
   assert.equal(learning.snapshot().busy.length > 0, true);
   location.hash = '#/space/s1/assessment?plan=p&task=other'; learning.sync(); releasePlan();
 }
 await starting;
 assert.equal(planReads, 1, 'new creation must validate the task route owner even after automatic recovery');
 if (planStatus === 'ready') {
   assert.equal(creates.length, 1); assert.equal(creates[0].plan_id, 'p'); assert.equal(creates[0].task_id, 't'); assert.equal(creates[0].learning_session_id, 'ss');
   assert.deepEqual(Array.from(creates[0].topic_ids), ['topic']); assert.equal(creates[0].question_count, 6); assert.deepEqual(Array.from(creates[0].question_types), ['short_answer']);
   assert.equal(creates[0].kind, 'retest');
 } else if (planStatus === 'route-changed') {
   assert.equal(creates.length, 0); assert.equal(state.assessmentContext, null, 'a late plan response cannot install the old owner into another task route');
   assert.equal(learning.snapshot().assessmentId, '');
 } else {
   assert.equal(creates.length, 0); assert.match(learning.snapshot().error, /历史记录|重新规划/);
   assert.equal(learning.snapshot().assessmentId, 'a'); assert.equal(learning.snapshot().assessmentStatus, 'completed');
 }
});

for (const id of ['', 'diagnostic']) test(`legacy learning diagnostic entry redirects to canonical gated recovery: ${id || 'default'}`, async () => {
 const space = { id: 's1' }, redirects = [], state = { loading: false, error: '', spaceTab: 'assessment', topics: new Map([['s1', []]]) };
 const location = { hash: '#/learning' + (id ? '/' + id : ''), replace: hash => redirects.push(hash) };
 let knowledgeReads = 0;
 const context = vm.createContext({ state, location, currentSpace: () => space, route: () => ({ name: 'learning', id }), spacePath,
   study: { dispose() {} }, notes: { dispose() {} },
   learning: { snapshot: () => ({ knowledge: {}, assessmentId: '' }), loadKnowledge: async () => { knowledgeReads++; } },
   render() {} });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 await context.loadRoute();
 assert.deepEqual(redirects, ['#/space/s1/assessment']); assert.equal(knowledgeReads, 0);
 assert.notEqual(state.assessmentReadyRoute, location.hash);
});

for (const alias of ['assessment_id', 'assessment']) test(`assessment route consumes ${alias} before creation defaults and validates exact query owner`, async () => {
 const calls = [], space = { id: 's1' }, hash = `#/space/s1/assessment?plan=p&task=t&${alias}=a`;
 const location = { hash }, state = { loading: false, error: '', spaceTab: 'assessment', learningSection: 'diagnostic', topics: new Map([['s1', []]]) };
 const learning = { snapshot: () => ({ setupRevision: 0, assessmentId: 'a', diagnostic: { status: 'in_progress' }, knowledge: {}, error: '' }),
   recover: async filter => calls.push(['recover', filter]), configureDefaults: () => { throw new Error('existing round must not receive creation defaults'); },
   initializeError: error => { throw error; } };
 const context = vm.createContext({ state, location, route: () => ({ name: 'space', section: 'assessment', query: { plan: 'p', task: 't', [alias]: 'a' } }), currentSpace: () => space,
   workbench: { load: async () => {} }, study: { dispose() {} }, notes: { dispose() {} }, learning,
   api: { plan: async () => { throw new Error('do not apply new-creation plan restrictions before recovery'); } }, render() {} });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 await context.loadRoute();
 assert.equal(calls.length, 1); assert.equal(calls[0][1].assessmentId, 'a');
 assert.equal(calls[0][1].planId, 'p'); assert.equal(calls[0][1].taskId, 't');
 assert.equal(state.assessmentReadyRoute, hash);
});

test('route initialization gates setup and a late plan default respects setup revision', async () => {
 const space = { id: 's1' }, location = { hash: '#/space/s1/assessment?plan=p&task=t' };
 let release, reached; const planWaiting = new Promise(resolve => { reached = resolve; });
 const state = { loading: false, error: '', spaceTab: 'assessment', learningSection: 'diagnostic', topics: new Map([['s1', []]]) };
 const { createLearningController } = await import('../src/learning.js');
 const api = { recoverableAssessments: async () => ({ items: [] }), space: async () => space, topics: async () => [],
   plan: async () => { reached(); return new Promise(resolve => { release = resolve; }); } };
 const learning = createLearningController({ api, storage: { getItem: () => null, setItem() {} },
   context: () => ({ spaceId: 's1', active: true, routeKey: location.hash, setupReady: state.assessmentReadyRoute === location.hash, assessmentContext: state.assessmentContext }) });
 const { taskAssessmentContext } = await import('../src/workbench.js');
 const context = vm.createContext({ state, location, currentSpace: () => space, route: () => ({ name: 'space', section: 'assessment', query: { plan: 'p', task: 't' } }),
   api, learning, taskAssessmentContext, workbench: { load: async () => {} }, study: { dispose() {} }, notes: { dispose() {} }, render() {} });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 const pending = context.loadRoute(); await planWaiting;
 assert.equal(learning.snapshot().setupReady, false);
 learning.configure({ kind: 'retest', count: '8' });
 release({ id: 'p', space_id: 's1', status: 'ready', tasks: [{ id: 't', status: 'pending', topic_ids: ['topic'] }] });
 await pending;
 assert.equal(learning.snapshot().setupReady, true); assert.equal(learning.snapshot().setup.kind, 'retest'); assert.equal(learning.snapshot().setup.count, '8');
});

test('concurrent initialization of the same assessment route only opens the gate after the latest request succeeds', async () => {
 const space = { id: 's1' }, location = { hash: '#/space/s1/assessment' };
 const state = { loading: false, error: '', spaceTab: 'assessment', learningSection: 'diagnostic', topics: new Map([['s1', []]]) };
 const requests = []; let creates = 0;
 const api = { space: async () => space, topics: async () => [{ id: 'topic', source_refs: [{}] }],
   createAssessment: async () => { creates++; return {}; },
   recoverableAssessments: async (_, filters, { signal }) => new Promise((resolve, reject) => {
     requests.push({ resolve, reject });
     signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true });
   }) };
 const { createLearningController } = await import('../src/learning.js');
 const learning = createLearningController({ api, storage: { getItem: () => null, setItem() {} },
   context: () => ({ spaceId: space.id, active: true, routeKey: location.hash, setupReady: state.assessmentReadyRoute === location.hash }) });
 const context = vm.createContext({ state, location, currentSpace: () => space, route: () => ({ name: 'space', section: 'assessment', query: {} }),
   api, learning, workbench: { load: async () => {} }, study: { dispose() {} }, notes: { dispose() {} }, render() {} });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 const first = context.loadRoute(); await new Promise(resolve => setImmediate(resolve));
 const second = context.loadRoute(); await new Promise(resolve => setImmediate(resolve));
 assert.equal(requests.length, 2);
 await first;
 assert.notEqual(state.assessmentReadyRoute, location.hash, 'superseded initialization cannot open setup while the newest index is pending');
 assert.equal(learning.snapshot().setupReady, false);
 requests[1].reject(new Error('index offline')); await second;
 assert.equal(learning.snapshot().error, 'index offline'); assert.equal(learning.snapshot().setupReady, false);
 await learning.start(); assert.equal(creates, 0);
 const retry = context.loadRoute(); await new Promise(resolve => setImmediate(resolve));
 requests[2].resolve({ items: [] }); await retry;
 assert.equal(state.assessmentReadyRoute, location.hash); assert.equal(learning.snapshot().setupReady, true);
 assert.equal(learning.snapshot().error, ''); assert.ok(learning.snapshot().knowledge);
});

test('assessment confirmation cannot replace a manual completion pending in the actual management controller', async () => {
 const stored = new Map(), storage = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) };
 const space = { id: 's', topic_ids: ['topic'] }, calls = [];
 const plan = { id: 'p', space_id: 's', version: 1, status: 'ready', tasks: [{ id: 't', status: 'pending', topic_ids: ['topic'] }] };
 const assessment = { assessmentId: 'a', assessment: { id: 'a', space_id: 's', plan_id: 'p', task_id: 't', status: 'completed' }, diagnostic: { assessment_id: 'a', status: 'completed' } };
 let release, navigations = 0;
 const api = { plan: async () => structuredClone(plan), space: async () => space, topics: async () => [], updateTask: async (p, t, body, options) => {
   calls.push({ p, t, body, key: options.key });
   if (calls.length === 1) throw Object.assign(new Error('提交结果未知'), { status: 503 });
   if (body.source === 'manual') return new Promise(resolve => { release = () => resolve({ id: t, status: 'completed', plan_version: 2 }); });
   return { id: t, status: 'completed', plan_version: 2 };
 } };
 const taskCompletion = createTaskCompletionController({ api, storage });
 const target = assessmentTaskCompletion(space, assessment, plan);
 taskCompletion.snapshot(target); // The result page was opened before the manual request.
 const workspace = createWorkspaceController({ api, storage, context: () => ({ route: 'task/p/t' }) });
 const context = vm.createContext({ location: { hash: '#/space/s/assessment' }, currentSpace: () => space, assessmentPageState: () => assessment, assessmentTaskCompletion, state: { plans: new Map([['s', plan]]) }, taskCompletion, continueAfterTaskConfirmation: async () => { navigations++; } });
 vm.runInContext(extract('async function confirmAssessmentTask()', '\nfunction resetTimer()'), context);
 const form = note => { const data = new FormData(); data.set('status', 'completed'); data.set('note', note); return data; };
 await workspace.load(); await workspace.submit(form('封存的手工备注'));
 await assert.rejects(context.confirmAssessmentTask(), /原完成确认结果未知.*手工/);
 assert.equal(calls.length, 1); assert.equal(taskCompletion.snapshot(target).pending.body.note, '封存的手工备注'); assert.equal(navigations, 0);
 const retry = workspace.submit(form('不能替换原备注'));
 const sameIntentRetry = taskCompletion.complete({ spaceId: 's', planId: 'p', taskId: 't', expectedPlanVersion: 9, source: 'manual' });
 const rejectedWhileSending = assert.rejects(context.confirmAssessmentTask(), /原完成确认结果未知.*手工/);
 assert.equal(calls.length, 2); release(); await Promise.all([retry, sameIntentRetry, rejectedWhileSending]);
 assert.deepEqual(calls[0], calls[1]); assert.equal(navigations, 0);
});

for (const scenario of ['next', 'session', 'replan', 'summary', 'assessment']) test(`task confirmation uses its receipt without waiting for aggregate progress: ${scenario}`, async () => {
 const space = { id: 's', topic_ids: ['topic'] }, calls = [];
 const plan = { id: 'p', status: scenario === 'replan' ? 'needs_replan' : 'ready', tasks: [{ id: 'done', status: 'completed' }, { id: 'next', status: scenario === 'summary' ? 'deferred' : 'pending' }] };
 let data = { current_plan: { id: 'stale', tasks: [{ id: 'wrong', status: 'pending' }] } };
 const fresh = { current_plan: plan, active_session: scenario === 'session' ? { session_id: 'active', plan_id: 'p', task_id: 'done' } : null };
 const location = { hash: '#/space/s/assessment' };
 const context = vm.createContext({ currentSpace: () => space, location, workbench: { invalidate: id => calls.push(['invalidate', id]), load: async id => { calls.push(['load', id]); data = fresh; }, snapshot: () => ({ data, review: { items: [{ review_schedule: { due: true } }] } }) },
   nextLearningStep, spacePath, studyPath, toast: () => {}, loadStudy: async () => {}, route: () => ({}), render: () => {} });
 assert.ok(app.includes('async function continueAfterTaskConfirmation('));
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 context.workbench.load = () => { calls.push(['load', 's']); return new Promise(() => {}); };
 const step = { kind: scenario === 'next' ? 'task' : scenario, plan_id: 'p', task_id: scenario === 'session' ? 'done' : 'next', assessment_id: 'a' };
 await Promise.race([context.continueAfterTaskConfirmation('s', { receipt: { space_id: 's', plan_id: 'p', plan_version: 2, next_action: step } }), new Promise((_, reject) => setTimeout(() => reject(new Error('navigation waited for aggregate reads')), 100))]);
 assert.equal(location.hash, scenario === 'replan' ? '#/space/s/plan' : scenario === 'summary' ? '#/space/s/summary' : scenario === 'assessment' ? '#/space/s/assessment?plan=p&task=next&assessment_id=a' : studyPath('s', scenario === 'session' ? 'done' : 'next', 'p'));
});

test('completion captures route before the write and does not steal another page in the same space', async () => {
 const location = { hash: '#/space/s/assessment?assessment_id=a' };
 const target = { spaceId: 's', planId: 'p', taskId: 't', expectedPlanVersion: 1, source: 'assessment', assessmentId: 'a' };
 let release, navigationArgs;
 const context = vm.createContext({ location, currentSpace: () => ({ id: 's' }), assessmentPageState: () => ({}), assessmentTaskCompletion: () => target,
   state: { plans: new Map() }, taskCompletion: { complete: () => new Promise(resolve => { release = resolve; }) },
   continueAfterTaskConfirmation: async (...args) => { navigationArgs = args; } });
 vm.runInContext(extract('async function confirmAssessmentTask()', '\nfunction resetTimer()'), context);
 const operation = context.confirmAssessmentTask();
 location.hash = '#/space/s/notes';
 release({ id: 't', space_id: 's', plan_id: 'p', plan_version: 2, next_action: { kind: 'summary', plan_id: 'p' } });
 await operation;
 assert.equal(navigationArgs[2], '#/space/s/assessment?assessment_id=a');
});

test('a receipt arriving after a same-space route change leaves navigation recoverable', async () => {
 const location = { hash: '#/space/s/notes' }; let pending = 0;
 const context = vm.createContext({ location, currentSpace: () => ({ id: 's' }), workbench: { invalidate() {}, load: async () => {} },
   taskCompletion: { markNavigationPending() { pending++; } } });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await context.continueAfterTaskConfirmation('s', { target: { spaceId: 's', planId: 'p', taskId: 't' }, receipt: { space_id: 's', plan_id: 'p', plan_version: 2, next_action: { kind: 'summary' } } }, '#/space/s/assessment');
 assert.equal(location.hash, '#/space/s/notes'); assert.equal(pending, 1);
});

test('the next study body opens from continuation while aggregate progress never settles', async () => {
 const space = { id: 's' }, plan = { id: 'p', space_id: 's', tasks: [{ id: 'next' }] }, location = { hash: '#/space/s/study?plan=p&task=next' };
 let loaded;
 const context = vm.createContext({ route: () => ({ name: 'space', id: 's', section: 'study', query: { plan: 'p', task: 'next' } }), location, currentSpace: () => space,
   completionContinuations: new Map([['s', { space_id: 's', current_plan: plan, active_session: null, task_id: 'next' }]]), state: { chatCapabilityError: '' }, data: () => ({ materials: [] }),
   workbench: { load: () => new Promise(() => {}), snapshot: () => ({ data: null }) },
   study: { load: async value => { loaded = value; }, snapshot: () => ({ mode: 'explanation', refs: [] }) } });
 vm.runInContext(extract('async function loadStudy(', '\nfunction updateRouteNodeStates('), context);
 await Promise.race([context.loadStudy(), new Promise((_, reject) => setTimeout(() => reject(new Error('study body blocked by aggregate reads')), 100))]);
 assert.equal(loaded.plan, plan); assert.equal(loaded.taskId, 'next');
 assert.equal(context.completionContinuations.has('s'), false, 'old continuation cannot survive undo or reordering on later visits');
});

test('actual route entry reaches the continuation before waiting for aggregate progress', async () => {
 const space = { id: 's' }, current = { name: 'space', id: 's', section: 'study', query: { plan: 'p', task: 'next' } };
 let opened = 0;
 const context = vm.createContext({ state: {}, route: () => current, currentSpace: () => space, location: { hash: '#/space/s/study?plan=p&task=next' },
   notes: { dispose() {} }, study: {}, completionContinuations: new Map([['s', { current_plan: { id: 'p' }, task_id: 'next' }]]),
   workbench: { load: () => new Promise(() => {}) }, loadStudy: async () => { opened++; } });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 await Promise.race([context.loadRoute(), new Promise((_, reject) => setTimeout(() => reject(new Error('route waits before handoff')), 100))]);
 assert.equal(opened, 1);
});

test('failed next-node body retains the saved completion recovery action', async () => {
 const target = { spaceId: 's', planId: 'p', taskId: 'done' }; let acknowledged = 0, pending;
 const context = vm.createContext({ currentSpace: () => ({ id: 's' }), location: { hash: '#/space/s/study?plan=p&task=next' },
   route: () => ({ query: { plan: 'p', task: 'next' } }), state: {}, data: () => ({ materials: [] }),
   completionContinuations: new Map([['s', { space_id: 's', current_plan: { id: 'p' }, task_id: 'next', completionTarget: target }]]),
   study: { load: async () => {}, snapshot: () => ({ task: { id: 'next' }, error: '正文读取失败', mode: 'explanation' }) },
   taskCompletion: { acknowledgeNavigation() { acknowledged++; }, markNavigationPending(t, error) { pending = { t, error }; } } });
 vm.runInContext(extract('async function loadStudy(', '\nfunction updateRouteNodeStates('), context);
 await context.loadStudy(); assert.equal(acknowledged, 0); assert.equal(pending.t, target); assert.equal(pending.error, '正文读取失败');
});

test('summary route renders its receipt while aggregate progress never settles', async () => {
 const location = { hash: '#/space/s/summary' }; let rendered = 0;
 const context = vm.createContext({ state: {}, location, route: () => ({ name: 'space', id: 's', section: 'summary' }), currentSpace: () => ({ id: 's' }),
   notes: { dispose() {} }, study: { dispose() {} }, render() { rendered++; },
   completionContinuations: new Map([['s', { kind: 'summary', destination: location.hash }]]), workbench: { load: () => new Promise(() => {}) } });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 await Promise.race([context.loadRoute(), new Promise((_, reject) => setTimeout(() => reject(new Error('summary blocked by progress')), 100))]);
 assert.equal(rendered, 1);
});

test('resuming an older active session uses a dedicated plan read without aggregate progress', async () => {
 const location = { hash: '#/space/s/assessment' }, plans = [], handoffs = new Map();
 const oldPlan = { id: 'old', space_id: 's', tasks: [{ id: 'active' }] };
 const context = vm.createContext({ location, currentSpace: () => ({ id: 's' }), completionContinuations: handoffs,
   state: { plans: new Map() }, api: { plan: async id => { plans.push(id); return oldPlan; } },
   workbench: { invalidate() {}, load: () => new Promise(() => {}) }, spacePath, studyPath });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await context.continueAfterTaskConfirmation('s', { receipt: { space_id: 's', plan_id: 'p', plan_version: 2,
   continuation_context: { space_id: 's', current_plan: { id: 'p' }, active_session: { plan_id: 'old', task_id: 'active' } },
   next_action: { kind: 'session', plan_id: 'old', task_id: 'active' } } });
 assert.deepEqual(plans, ['old']); assert.equal(location.hash, studyPath('s', 'active', 'old'));
 assert.equal(handoffs.get('s').current_plan, oldPlan); assert.equal(handoffs.get('s').current, false);
});

test('leaving and returning to the same hash does not give a late completion ownership again', async () => {
 let pending = 0; const location = { hash: '#/space/s/assessment' };
 const context = vm.createContext({ routeVersion: 3, location, currentSpace: () => ({ id: 's' }),
   taskCompletion: { markNavigationPending() { pending++; } } });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await context.continueAfterTaskConfirmation('s', { target: { spaceId: 's', planId: 'p', taskId: 't' }, receipt: { space_id: 's', next_action: { kind: 'summary' } } }, location.hash, 1);
 assert.equal(pending, 1); assert.equal(location.hash, '#/space/s/assessment');
});

test('confirmation with failed progress refresh stays put and exposes the saved status', async () => {
 const location = { hash: '#/space/s/assessment' };
 const context = vm.createContext({ currentSpace: () => ({ id: 's' }), location, workbench: { invalidate() {}, load: async () => {}, snapshot: () => ({ data: null, error: 'offline' }) } });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await assert.rejects(context.continueAfterTaskConfirmation('s'), /任务完成已保存.*读取失败/);
 assert.equal(location.hash, '#/space/s/assessment');
});

test('confirmation does not navigate from a retained stale projection after refresh failure', async () => {
 const space = { id: 's', topic_ids: ['topic'] }, location = { hash: '#/space/s/assessment' };
 const context = vm.createContext({ currentSpace: () => space, location, workbench: { invalidate() {}, load: async () => {}, snapshot: () => ({ data: { current_plan: { id: 'p', tasks: [{ id: 'already-saved', status: 'pending' }] } }, stale: true, status: 'error', error: 'offline' }) }, nextLearningStep, spacePath, studyPath });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await assert.rejects(context.continueAfterTaskConfirmation('s'), /任务完成已保存.*读取失败/);
 assert.equal(location.hash, '#/space/s/assessment');
});

for (const failed of [false, true]) test(`saving a task on the real management callbacks preserves 21 route nodes and scopes refreshes: failure=${failed}`, async () => {
 const space = { id: 's1', name: '21节点夹具', topic_ids: ['topic'] }, other = { id: 's2', topic_ids: ['other'] }, spaces = [space, other];
 let current = { id: 'p1', space_id: 's1', version: 1, status: 'ready', tasks: Array.from({ length: 21 }, (_, index) => ({ id: `t${index}`, node_id: `n${index}`, status: 'pending', topic_ids: ['topic'], sequence: index + 1 })) };
 let release, delayed, refresh = false, lists = 0, clears = 0, writes = 0, failRefresh = failed;
 const data = id => ({ space_id: id, current_plan: id === 's1' ? structuredClone(current) : { ...structuredClone(current), id: 'p2', space_id: id } });
 const progress = id => ({ space_id: id, current_plan_id: id === 's1' ? 'p1' : 'p2', current_plan_version: current.version, completed_count: id === 's1' ? current.tasks.filter(task => task.status === 'completed').length : 0, total_count: 21, rounds: [{ plan_id: id === 's1' ? 'p1' : 'p2', nodes: current.tasks.map(task => ({ ...task, task_id: task.id, plan_id: id === 's1' ? 'p1' : 'p2' })) }] });
 const api = { space: async id => spaces.find(space => space.id === id), topics: async () => [], plan: async () => structuredClone(current),
   spaces: async () => { lists++; return spaces; }, materials: async () => { lists++; return []; },
   workbench: async id => { const result = data(id); if (refresh && id === 's1') { await delayed; if (failRefresh) throw new Error('刷新503'); } return result; },
   progress: async id => progress(id), updateTask: async () => { writes++; current = { ...current, version: 2, tasks: current.tasks.map((task, index) => index ? task : { ...task, status: 'completed' }) }; return { ...current.tasks[0], plan_version: 2 }; },
 };
 const stored = new Map(), session = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) }, controllers = {}, rendered = [];
 const state = { selected: 's1', live: { spaces, materials: [] }, topics: new Map([['s1', ['topic']], ['s2', ['other']]]), plans: new Map(), graphSpace: 's2', graph: { nodes: ['other'] } };
 let context;
 context = vm.createContext({ api, session, state, mergeSpaceList, mergeSpaceProjection, route: () => ({ name: 'manage', id: 'task/p1/t0' }), byId: id => spaces.find(space => space.id === id), currentSpace: () => space,
   createWorkbenchController: options => controllers.workbench = createWorkbenchController(options), createWorkspaceController: options => controllers.workspace = createWorkspaceController(options),
   study: { snapshot: () => ({}) }, learning: { clear: () => clears++ }, renderProgress, currentProgressNodes, taskDisplay, esc,
   button: (label, action) => `<button data-action="${action}">${label}</button>`, notice: message => `<p role="alert">${message}</p>`,
   scheduleMaterialRefresh() {}, render: () => { if (controllers.workspace?.snapshot().data?.space) rendered.push(context.studyProgressPanel(space)); },
 });
 vm.runInContext(extract('const workbench = createWorkbenchController(', '\nconst taskCompletion ='), context);
 vm.runInContext(extract('const workspace = createWorkspaceController(', '\nconst manageLink ='), context);
 vm.runInContext(extract('function studyProgressPanel(', '\nfunction spaceMaterialsPanel('), context);
 vm.runInContext(extract('function progressForSpace(', '\nfunction nextActionHtml('), context);
 await Promise.all([controllers.workbench.load('s1'), controllers.workbench.load('s2')]); await controllers.workspace.load();
 const otherEntry = controllers.workbench.snapshot('s2');
 delayed = new Promise(resolve => { release = resolve; }); refresh = true;
 const form = new FormData(); form.set('status', 'completed');
 const saving = controllers.workspace.submit(form); await new Promise(resolve => setImmediate(resolve));
 assert.equal(controllers.workbench.snapshot('s1').data?.current_plan.tasks.length, 21);
 assert.equal(controllers.workbench.snapshot('s2'), otherEntry);
 assert.equal(state.topics.get('s2')[0], 'other'); assert.equal(state.graphSpace, 's2');
 release(); await saving;
 assert.equal(lists, 0); assert.equal(clears, 0); assert.equal(controllers.workspace.snapshot().route.page, 'task');
 assert.ok(rendered.length > 0); assert.ok(rendered.every(html => !/0 \/ 0|尚未生成当前计划/.test(html)));
 assert.equal(controllers.workbench.snapshot('s1').progress.total_count, 21);
 if (failed) {
   assert.match(controllers.workspace.snapshot().notice, /已保存/); assert.match(controllers.workspace.snapshot().error, /刷新失败/);
   assert.match(rendered.at(-1), /data-action="workbench-retry"/);
   assert.equal(controllers.workspace.snapshot().data.task.status, 'completed'); assert.equal(controllers.workspace.snapshot().data.plan.version, 2);
   failRefresh = false; await controllers.workbench.load('s1', { force: true }); await new Promise(resolve => setImmediate(resolve));
   assert.equal(controllers.workspace.snapshot().error, ''); assert.equal(controllers.workspace.snapshot().data.task.status, 'completed');
   assert.equal(controllers.workspace.snapshot().data.plan.version, 2); assert.equal(writes, 1);
   assert.match(rendered.at(-1), /1 \/ 21 项已完成/); assert.doesNotMatch(rendered.at(-1), /刷新失败/);
 } else {
   assert.equal(controllers.workbench.snapshot('s1').progress.completed_count, 1);
   assert.match(rendered.at(-1), /1 \/ 21 项已完成/); assert.match(rendered.at(-1), /已完成 1 \/ 21/);
 }
});

test('directly opening task management loads its workbench projection before finishing the route', async () => {
 const space = { id: 's1' }, calls = [], location = { hash: '#/manage/task/p1/t1' };
 const context = vm.createContext({ state: { loading: false, error: '' }, location, route: () => ({ name: 'manage', id: 'task/p1/t1' }), currentSpace: () => space, study: { dispose() {} }, notes: { dispose() {} },
   workspace: { load: async () => calls.push('workspace'), snapshot: () => ({ route: { page: 'task' }, data: { space } }) }, workbench: { load: async id => calls.push(`workbench:${id}`) }, render() {} });
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'), context);
 await context.loadRoute(); assert.deepEqual(calls, ['workspace', 'workbench:s1']);
});

test('study waits for the workbench before selecting a task and keeps stale failures out of task selection', async () => {
 const space = { id: 's1', topic_ids: ['topic'] }, location = { hash: '#/space/s1/study' }, loads = [];
 let release, entry = { data: null, status: 'loading', stale: true };
 const waiting = new Promise(resolve => { release = resolve; });
 const context = vm.createContext({ state: { chatCapabilityError: '' }, location, route: () => ({ name: 'space', id: 's1', section: 'study', query: {} }), currentSpace: () => space, data: () => ({ materials: [] }), nextLearningStep,
   workbench: { load: async () => waiting, snapshot: () => entry }, study: { load: async options => loads.push(options), snapshot: () => ({ mode: 'original', refs: [] }) }, render() {} });
 vm.runInContext(extract('async function loadStudy(', '\nfunction updateRouteNodeStates('), context);
 const selecting = context.loadStudy(); await new Promise(resolve => setImmediate(resolve)); assert.equal(loads.length, 0);
 entry = { data: { current_plan: { id: 'p1', status: 'ready', tasks: [{ id: 't1', status: 'pending' }] } }, status: 'ready', stale: false };
 release(); await selecting; assert.equal(loads[0].plan.id, 'p1');
 entry = { ...entry, status: 'error', stale: true, error: '刷新失败' };
 await context.loadStudy(); assert.equal(loads.length, 1);
});

test('plan and next-action placeholders reserve empty-plan wording for ready_empty', () => {
 for (const status of ['not_loaded', 'loading', 'error']) {
   const space = { id: 's1', topic_ids: ['topic'] }, entry = { data: null, status, busy: status === 'loading', error: status === 'error' ? '离线' : '' };
   const context = vm.createContext({ state: { plans: new Map() }, currentSpace: () => space, activeTasks: () => [], route: () => ({ query: {} }), workbench: { snapshot: () => entry },
     button: label => label, link: label => label, notice: text => text, empty: title => title, pageHeading: title => title, spaceSelect: () => '', focusCard: () => '', esc, renderProgress,
     progressForSpace: () => null, nextLearningStep, spacePath });
   vm.runInContext(extract('function planPage(', '\nfunction chatMessages('), context);
   vm.runInContext(extract('function nextActionHtml(', '\nfunction spaceOverviewPanel('), context);
   assert.doesNotMatch(context.planPage({ embedded: true }), /0 \/ 0|先生成一组|生成学习任务/);
   assert.doesNotMatch(context.nextActionHtml(space), /生成学习计划/);
 }
});

test('a direct study route renders projection loading and errors before any empty-plan invitation', () => {
 const space = { id: 's1' };
 for (const status of ['not_loaded', 'loading', 'error', 'ready', 'ready_empty']) {
   const entry = { status, stale: status === 'error', error: status === 'error' ? '工作台离线' : '', data: status.startsWith('ready') ? { current_plan: status === 'ready_empty' ? null : { id: 'p1' } } : null };
   const context = vm.createContext({ state: { plans: new Map(), topics: new Map() }, byId: () => space, route: () => ({ name: 'space', section: 'study' }), routeState: () => ({ tab: 'study', learningSection: 'diagnostic' }),
     data: () => ({ materials: [] }), materialIds: () => [], study: { snapshot: () => ({}) }, workbench: { snapshot: () => entry }, renderStudy, spaceShell: (_, body) => body, notice: message => message, esc,
     button: (label, action) => `<button data-action="${action}">${label}</button>` });
   vm.runInContext(extract('function spacePage(', '\nfunction openSpaceTab('), context);
   const html = context.spacePage('s1');
   if (status === 'ready_empty') assert.match(html, /生成学习计划/);
   else assert.doesNotMatch(html, /从一组学习节点开始|生成学习计划/);
   if (status === 'error') { assert.match(html, /工作台离线/); assert.match(html, /workbench-retry/); }
 }
});

test('workbench retry resumes the failed direct study load once and ignores a changed route', async () => {
 const space = { id: 's1', topic_ids: ['topic'] }, location = { hash: '#/space/s1/study' }, loads = [];
 let offline = true, reads = 0, waiting;
 const projection = createWorkbenchController({ api: { workbench: async () => {
   reads++; if (waiting) await waiting; if (offline) throw new Error('离线');
   return { space_id: 's1', current_plan: { id: 'p1', space_id: 's1', version: 1, status: 'ready', tasks: [{ id: 't1', status: 'pending' }] } };
 }, progress: async () => ({ space_id: 's1', current_plan_id: 'p1', current_plan_version: 1, total_count: 1, rounds: [] }) } });
 const context = vm.createContext({ state: { chatCapabilityError: '' }, location, route: () => ({ name: 'space', id: 's1', section: 'study', query: {} }), currentSpace: () => space, data: () => ({ materials: [] }), nextLearningStep,
   workbench: projection,
   study: { load: async options => loads.push(options), snapshot: () => ({ mode: 'original', refs: [] }) }, render() {} });
 vm.runInContext(extract('async function loadStudy(', '\nfunction updateRouteNodeStates('), context);
 const retrySource = extract("    'workbench-retry':", "    'task-assessment':");
 vm.runInContext(`var retry = ({${retrySource}})['workbench-retry'];`, context);
 await context.loadStudy(); assert.equal(loads.length, 0); assert.equal(reads, 1);
 await context.retry(); assert.equal(loads.length, 0); assert.equal(reads, 2);
 offline = false; await context.retry(); assert.equal(loads.length, 1); assert.equal(loads[0].plan.id, 'p1'); assert.equal(reads, 3);
 let release; waiting = new Promise(resolve => { release = resolve; });
 const retrying = context.retry(); await new Promise(resolve => setImmediate(resolve)); location.hash = '#/spaces'; release(); await retrying;
 assert.equal(loads.length, 1);
});

for (const selection of ['task', 'node', 'plan', 'default', 'same']) test(`study retry resolves the current ${selection} selection while preserving a matching reader`, async () => {
 const space = { id: 's1', topic_ids: ['topic'], bindings: [{ material_id: 'm', material_version_id: 'v' }] };
 const initial = { id: 'p1', space_id: 's1', version: 1, status: 'ready', tasks: [{ id: 't1', node_id: 'n1', status: 'pending' }, { id: 't2', node_id: 'n2', status: 'pending' }] };
 const plan = selection === 'default' ? { ...initial, version: 2, tasks: [{ ...initial.tasks[0], status: 'completed' }, initial.tasks[1]] } : initial;
 const query = selection === 'task' ? { task: 't2', plan: 'p1' } : selection === 'node' ? { node: 'n2' } : selection === 'plan' ? { task: 't1', plan: 'p2' } : selection === 'same' ? { task: 't1', plan: 'p1' } : {};
 const current = { name: 'space', id: 's1', section: 'study', query }, location = { hash: '#/space/s1/study?' + new URLSearchParams(query) };
 let offline = true, records = 0, sources = 0, files = 0;
 const api = { workbench: async () => { if (offline) throw new Error('工作台离线'); return { space_id: 's1', current_plan: plan }; },
   progress: async () => ({ space_id: 's1', current_plan_id: 'p1', current_plan_version: plan.version, total_count: 2, rounds: [] }), plan: async id => ({ ...initial, id }),
   learningRecord: async () => { records++; return { space_id: 's1', topics: [{ id: 'topic', source_refs: [{ material_id: 'm', material_version_id: 'v', chunk_id: 'c' }] }], sessions: [] }; }, studyLessons: async () => ({ items: [] }),
   source: async () => { sources++; return { material_name: '原文' }; }, materialFile: async () => { files++; return new Response('原始文件', { headers: { 'Content-Type': 'text/plain' } }); } };
 const controller = createStudyController({ api }), projection = createWorkbenchController({ api });
 try {
   await controller.load({ space, plan: initial, taskId: 't1' });
   await controller.readSource();
   const reader = controller.snapshot().reader, questionDraft = '保留原文问题';
   reader.iframe = {}; reader.scrollTop = 380; controller.question(questionDraft);
   const context = vm.createContext({ state: { chatCapabilityError: '' }, location, route: () => current, currentSpace: () => space, data: () => ({ materials: [] }), nextLearningStep,
     api, study: controller, workbench: projection, render() {} });
   vm.runInContext(extract('async function loadStudy(', '\nfunction updateRouteNodeStates('), context);
   const retrySource = extract("    'workbench-retry':", "    'task-assessment':");
   vm.runInContext(`var retry = ({${retrySource}})['workbench-retry'];`, context);
   await context.loadStudy(); assert.equal(records, 1); assert.equal(controller.snapshot().task.id, 't1');
   offline = false; await context.retry();
   assert.equal(controller.snapshot().task.id, ['task', 'node', 'default'].includes(selection) ? 't2' : 't1');
   assert.equal(controller.snapshot().plan.id, selection === 'plan' ? 'p2' : 'p1');
   assert.equal(records, selection === 'same' ? 1 : 2);
   assert.equal(sources, selection === 'same' ? 1 : 2); assert.equal(files, selection === 'same' ? 1 : 2);
   if (selection === 'same') { assert.equal(controller.snapshot().reader, reader); assert.equal(reader.scrollTop, 380); assert.equal(controller.snapshot().questionDraft, questionDraft); }
   else assert.notEqual(controller.snapshot().reader, reader);
 } finally { controller.dispose(); }
});

test('a late confirmation refresh cannot redirect another selected space', async () => {
 const location = { hash: '#/space/other/study' };
 const context = vm.createContext({ currentSpace: () => ({ id: 'other' }), location, workbench: { invalidate() {}, load: async () => {}, snapshot() { assert.fail('stale confirmation must not navigate'); } } });
 vm.runInContext(extract('async function continueAfterTaskConfirmation(', '\nasync function completeTask('), context);
 await context.continueAfterTaskConfirmation('s'); assert.equal(location.hash, '#/space/other/study');
});
test('notes reading never floats the study route over the chapter heading', () => {
 const dock={inert:false,classList:{remove(name){this.removed=name;}},setAttribute(name,value){this[name]=value;}};
 let mounted=0,disposed=0;
 const context=vm.createContext({route:()=>({name:'space',section:'notes'}),$:(selector)=>selector==='[data-study-progress-dock]'?dock:{getBoundingClientRect:()=>({bottom:72})},progressDock:{mount(){mounted++;},dispose(){disposed++;}}});
 vm.runInContext(extract('function updateProgressPosition()', '\nasync function openLearningRecord'),context);
 context.updateProgressPosition();assert.equal(mounted,0);assert.equal(disposed,1);assert.equal(dock.inert,true);assert.equal(dock['aria-hidden'],'true');assert.equal(dock.classList.removed,'visible');
});

test('opening a note chapter scrolls and focuses the loaded body without a stale navigation stealing focus', async () => {
 let focused=0,scrolled=0,resolveLoad;
 const hash='#/space/s/notes?chapter=c';
 const chapter={setAttribute(){},focus(){focused++;},scrollIntoView(){scrolled++;}};
 const context=vm.createContext({state:{loading:false,error:''},route:()=>({name:'space',id:'s',section:'notes',query:{chapter:'c'}}),currentSpace:()=>({id:'s'}),location:{hash},study:{dispose(){}},notes:{load:async()=>{},snapshot:()=>({chapter:{id:'c'}})},$:selector=>selector==='.notes-chapter'?chapter:null});
 vm.runInContext(extract('async function loadRoute()', '\nfunction newSpaceModal'),context);
 await context.loadRoute();assert.equal(focused,1);assert.equal(scrolled,1);
 context.notes.load=()=>new Promise(resolve=>{resolveLoad=resolve;});
 const pending=context.loadRoute();context.location.hash='#/materials';resolveLoad();await pending;
 assert.equal(focused,1);assert.equal(scrolled,1);
});
test('pause/resume updates existing route DOM and leaves reader untouched',()=>{
 const classes=new Map(), label={textContent:''}; let aria='1. test，学习中';
 const element={dataset:{node:'n'},classList:{toggle:(key,value)=>classes.set(key,value)},setAttribute:(key,value)=>{if(key==='aria-label')aria=value;},getAttribute:()=>aria};
 const session={session_id:'s',plan_id:'p',task_id:'t',status:'active',paused:false};
 const context=vm.createContext({study:{snapshot:()=>({space:{id:'space'},nodeId:'n',activeSession:session})},currentSpace:()=>({id:'space'}),workbench:{snapshot:()=>({data:{current_plan:{id:'p'}}})},currentProgressNodes:()=>[{node_id:'n',task_id:'t',plan_id:'p',status:'pending',active_session_id:'s'}],progressForSpace:()=>({}),nodeSessionState,document:{querySelectorAll:()=>[element]},$:selector=>selector==='.progress-node-status' ? label : null});
 vm.runInContext(extract('function updateRouteNodeStates()', '\nfunction syncAssessmentTicker'),context);
 context.updateRouteNodeStates(); assert.equal(classes.get('running'),true);
 session.paused=true;context.updateRouteNodeStates();assert.equal(classes.get('running'),false);assert.equal(classes.get('paused'),true);assert.equal(label.textContent,'已暂停');assert.match(aria,/已暂停/);
 session.paused=false;context.updateRouteNodeStates();assert.equal(classes.get('running'),true);
});
test('elapsed updates only existing generation label',()=>{
 const label={dataset:{assessmentStage:'validating'},textContent:''};const view={generationStartedAt:1000};
 const context=vm.createContext({learning:{snapshot:()=>view},document:{querySelectorAll:()=>[label]},assessmentStageText:(stage,time)=>`${stage}:${time}`});
 vm.runInContext(extract('function updateAssessmentElapsed()', '\nfunction updateStudy'),context);context.updateAssessmentElapsed();assert.equal(label.textContent,'validating:1000');
});
test('session-only pause updates badge, existing controls and footer without rebuilding reading workspace',()=>{
 const badge={textContent:''},footer={textContent:''},control={dataset:{studyAction:'pause'},textContent:'暂停',setAttribute(key,value){this.attribute=[key,value];}};
 const host={dataset:{studyKey:'key'},reader:{},scrollTop:250};const original=host.reader;
 const view={key:'key',space:{id:'space'},task:{id:'t'},session:{status:'active',task_id:'t',paused:true}};
 let nodeUpdates=0;
 const context=vm.createContext({route:()=>({name:'space',section:'study',id:'space'}),study:{snapshot:()=>view},updateRouteNodeStates:()=>nodeUpdates++,$:(selector)=>selector==='.study-workspace'?host:selector==='.study-task .badge'?badge:selector.includes('study-task-actions')?control:selector==='.study-footer > div > strong'?footer:null,render(){assert.fail('must not rerender page');},renderStudy(){assert.fail('must not rebuild workspace');}});
 vm.runInContext(extract('function updateStudy(', '\nfunction captureStudySidebarScroll'),context);
 context.updateStudy({sessionOnly:true});assert.equal(nodeUpdates,1);assert.equal(host.reader,original);assert.equal(host.scrollTop,250);assert.equal(badge.textContent,'学习已暂停');assert.equal(control.dataset.studyAction,'resume');assert.equal(control.textContent,'继续学习');assert.equal(footer.textContent,'当前会话已暂停');
 view.session.paused=false;context.updateStudy({sessionOnly:true});assert.equal(control.dataset.studyAction,'pause');assert.equal(badge.textContent,'正在学习');assert.equal(footer.textContent,'学习会话进行中');
});
test('a changed study key takes full render instead of recursively preserving the old mounted workspace',()=>{
 const mounted={dataset:{studyKey:'old-key'}};let updates=0;
 const context=vm.createContext({location:{hash:'#/space/space/study?task=t4&plan=p'},route:()=>({name:'space',id:'space',section:'study',query:{task:'t4',plan:'p'}}),study:{snapshot:()=>({key:'new-key',space:{id:'space'},task:{id:'t4'},plan:{id:'p'}})},state:{},$:(selector)=>selector==='.study-workspace'?mounted:null,byId:()=>({id:'space'}),routeState:()=>({tab:'study',learningSection:'diagnostic'}),updateStudy:()=>{updates++;},learning:{sync(){throw new Error('full-render-reached');}}});
 vm.runInContext(extract('function render()', 'const pendingMaterialStatuses'),context);
 assert.throws(()=>context.render(),/full-render-reached/);assert.equal(updates,0);
});

test('mounted study render updates refresh feedback and cumulative progress without replacing the reader', () => {
 const space = { id: 's1' }, reader = { scrollTop: 320, iframe: {} }, mounted = { dataset: { studyKey: 'study' }, reader, scrollTop: 480 }, originalFrame = reader.iframe;
 const plan = { id: 'p1', space_id: 's1', version: 1, status: 'ready', tasks: Array.from({ length: 21 }, (_, index) => ({ id: `t${index}`, node_id: `n${index}`, status: 'pending', sequence: index + 1 })) };
 const progress = { space_id: 's1', completed_count: 0, total_count: 21, rounds: [{ plan_id: 'p1', nodes: plan.tasks.map(task => ({ ...task, task_id: task.id, plan_id: 'p1' })) }] };
 const entry = { data: { current_plan: plan }, progress, status: 'ready', busy: false, error: '' };
 let panel, dock = { replaceWith(value) { dock = value; } }, context;
 const makePanel = html => ({ outerHTML: html.split('<aside class="panel study-progress-dock"')[0], scroller: { scrollLeft: 0 }, history: { open: false },
   querySelectorAll: () => plan.tasks.map(task => ({ dataset: { node: task.node_id } })), replaceWith(value) { panel = value; } });
 const select = (selector, root) => {
   if (selector === '.study-workspace') return mounted;
   if (selector === '[data-study-progress]') return root?.panel || panel;
   if (selector === '[data-study-progress-dock]') return root?.dock || dock;
   if (selector === '.progress-scroll') return root?.scroller;
   if (selector === '.study-progress-history') return root?.history;
   return null;
 };
 context = vm.createContext({ state: { plans: new Map() }, location: { hash: '#/space/s1/study?task=t0&plan=p1' }, route: () => ({ name: 'space', id: 's1', section: 'study', query: { task: 't0', plan: 'p1' } }), currentSpace: () => space,
   study: { snapshot: () => ({ key: 'study', space, task: plan.tasks[0], plan }) }, workbench: { snapshot: () => entry }, progressForSpace: () => entry.progress,
   renderProgress, currentProgressNodes, taskDisplay, esc, $: select, header() {}, updateStudy() {}, updateRouteNodeStates() {}, updateProgressPosition() {},
   button: (label, action) => `<button data-action="${action}">${label}</button>`, notice: message => `<p role="alert">${message}</p>`,
   document: { createElement: () => ({ set innerHTML(html) { this.panel = makePanel(html); this.dock = { replaceWith(value) { dock = value; } }; } }) },
   main: { set innerHTML(_) { assert.fail('mounted reader must not be rebuilt'); } },
 });
 vm.runInContext(extract('function studyProgressPanel(', '\nfunction spaceMaterialsPanel('), context);
 vm.runInContext(extract('function render()', 'const pendingMaterialStatuses'), context);
 panel = makePanel(context.studyProgressPanel(space)); panel.scroller.scrollLeft = 145; panel.history.open = true;
 entry.status = 'error'; entry.error = '刷新503'; context.render();
 assert.match(panel.outerHTML, /data-progress-status="error"/); assert.match(panel.outerHTML, /data-action="workbench-retry"/);
 entry.status = 'ready'; entry.error = ''; plan.version = 2; plan.tasks[0].status = 'completed'; progress.completed_count = 1; progress.rounds[0].nodes[0].status = 'completed'; context.render();
 assert.match(panel.outerHTML, /data-progress-status="ready"/); assert.match(panel.outerHTML, /已完成 1 \/ 21/); assert.doesNotMatch(panel.outerHTML, /刷新失败|workbench-retry/);
 assert.equal(panel.scroller.scrollLeft, 145); assert.equal(panel.history.open, true);
 assert.equal(mounted.reader, reader); assert.equal(reader.iframe, originalFrame); assert.equal(reader.scrollTop, 320); assert.equal(mounted.scrollTop, 480);
});

for(const operation of ['archive','restore']) test(`space ${operation} invalidates same task through the actual app submit handler`,async()=>{
 const task={id:'t',status:'completed',topic_ids:['topic'],context:{completion:{event_id:'e1'}}};
 let status=operation==='restore'?'archived':'active',reads=0,current='task/p/t';
 const space={id:'s',name:'Space',status,space_version:1};
 const state={live:{spaces:[space]},selected:'s'};
 const map=new Map(),session={getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v)};
 const workspace=createWorkspaceController({storage:session,context:()=>({route:current}),api:{
  plan:async()=>{reads++;return {id:'p',space_id:'s',version:2,status:'ready',tasks:[task]};},
  space:async()=>({...space,status}),topics:async()=>[]}});
 await workspace.load();current='';
 const context=vm.createContext({state,workspace,api:{},modalVersion:1,modal:{open:false},FormData:class {get(){return 'on';}},
  changeSpace:async()=>{status=operation==='archive'?'archived':'active';return {...space,status,space_version:2};},
  byId:id=>state.live.spaces.find(s=>s.id===id),mergeSpaceProjection,storageWrite,session,route:()=>({name:'spaces'}),render(){},toast(){}});
 vm.runInContext(extract('async function submitSpaceChange(', '\nfunction spaceCard('),context);
 await context.submitSpaceChange({dataset:{operation,space:'s',name:'Space',version:'1'}});
 current='task/p/t';await workspace.load();
 assert.equal(reads,2);assert.equal(workspace.snapshot().data.space.status,status);
 const {renderWorkspace}=await import('../src/workspace-view.js');
 if(operation==='restore')assert.match(renderWorkspace(workspace.snapshot()),/data-workspace-action="undo-open"/);
 else assert.doesNotMatch(renderWorkspace(workspace.snapshot()),/data-workspace-action="undo-open"/);
});

for(const followupFails of [false,true]) test(`accepted replacement retires old task before follow-up read, failure=${followupFails}`,async()=>{
 let current='task/old/t',status='ready',reads=0,failOld=false;
 const old={id:'old',space_id:'s',status:'ready',version:2,tasks:[{id:'t',status:'completed',topic_ids:['topic'],context:{completion:{event_id:'E1'}}}]};
 const workspace=createWorkspaceController({context:()=>({route:current}),api:{plan:async()=>{reads++;if(failOld)throw new Error('old read offline');return {...structuredClone(old),status};},space:async()=>({id:'s',status:'active'}),topics:async()=>[]}});
 await workspace.load();current='';
 const context=vm.createContext({state:{plans:new Map([['s',old]])},workspace,byId:()=>({id:'s',status:'active'}),
  modalVersion:1,routeVersion:0,location:{hash:'#/space/s/plan'},modal:{open:true},currentSpace:()=>({id:'s'}),
  FormData:class{get(k){return {schedule_mode:'session_window',order_mode:'source'}[k]||null;}has(){return false;}},
  planSettings:()=>({sessions:{valid:true,value:3},minutes:{valid:true,value:25}}),$:()=>null,
  api:{createPlan:async()=>{status='superseded';return {plan_id:'new',status:'ready'};},plan:async()=>{if(followupFails)throw new Error('new read offline');return {id:'new',space_id:'s',status:'ready',version:1,tasks:[]};}},
  storageRead:()=>({}),storageWrite(){},session:{},resetTimer(){},closeModal(){},workbench:{invalidate(){},load:async()=>{}},openSpaceTab(){},toast(){}});
 vm.runInContext(extract('async function createPlan(', '\nasync function continueAfterTaskConfirmation('),context);
 const accepted=context.createPlan({isConnected:true,dataset:{space:'s'},querySelectorAll:()=>[]});
 if(followupFails)await assert.rejects(accepted,/new read offline/);else await accepted;
 current='task/old/t';assert.equal(workspace.snapshot().data,null,'accepted replacement must retire cache before a follow-up error');
 current='task/old/t';failOld=true;await workspace.load();assert.equal(workspace.snapshot().data,null);assert.match(workspace.snapshot().error,/offline/);
 await workspace.action('undo-open');assert.notEqual(workspace.snapshot().taskUndo?.open,true);
 failOld=false;await workspace.load();assert.equal(reads,3);assert.equal(workspace.snapshot().data.plan.status,'superseded');
 const {renderWorkspace}=await import('../src/workspace-view.js');assert.doesNotMatch(renderWorkspace(workspace.snapshot()),/data-workspace-action="undo-open"/);
});

test('actual hash navigation retires cached ready task and late read while preserving unknown undo intent',async()=>{
 let current='task/old/t',status='ready',release,delayed=false;const calls=[];
 const saved=new Map(),storage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
 const plan={id:'old',space_id:'s',status:'ready',version:2,tasks:[{id:'t',status:'completed',topic_ids:['topic'],context:{completion:{event_id:'E1'}}}]};
 const workspace=createWorkspaceController({storage,context:()=>({route:current}),api:{plan:async()=>{const result={...structuredClone(plan),status};if(delayed)await new Promise(resolve=>{release=resolve;});return result;},space:async()=>({id:'s',status:'active'}),topics:async()=>[],undoTaskCompletion:async(p,t,body,options)=>{calls.push({body,key:options.key});throw new Error('lost');}}});
 await workspace.load();await workspace.action('undo-open');const values=new FormData();values.set('operation','undo_completion');values.set('confirm','on');values.set('reason','original draft');await workspace.submit(values);
 let onHash;const context=vm.createContext({workspace,state:{},routeVersion:0,modal:{open:false},render(){},main:{focus(){}},window:{addEventListener:(name,callback)=>{onHash=callback;},scrollTo(){}},loadRoute(){}});
 const start=app.indexOf("window.addEventListener('hashchange'");vm.runInContext(app.slice(start,app.indexOf('\n',start)),context);
 delayed=true;const oldRead=workspace.load(true);while(!release)await new Promise(resolve=>setImmediate(resolve));
 current='';onHash();status='superseded';current='task/old/t';onHash();delayed=false;await workspace.load();release();await oldRead;
 assert.equal(workspace.snapshot().data.plan.status,'superseded');assert.equal(calls.length,1,'navigation must never post unknown intent');
 const {renderWorkspace}=await import('../src/workspace-view.js');assert.match(renderWorkspace(workspace.snapshot()),/重试原撤销/);
 values.set('reason','edited draft');await workspace.submit(values);assert.deepEqual(calls[1],calls[0]);
});
