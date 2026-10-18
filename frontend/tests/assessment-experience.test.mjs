import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { createLearningController, renderLearning } from '../src/learning.js';
import { renderWorkspace } from '../src/workspace-view.js';
import { renderAssistanceReasons, renderMasteryGaps } from '../src/assessment-presentation.js';
const memory = () => { const data = new Map(); return { getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v) }; };
const context = () => ({ spaceId: 's1', active: true });
const question = { id: 'q1', type: 'single_choice', prompt: '**Prompt**\n\n- item\n\n```js\nconst a = 1;\n```', options: [{ id:'A', text: '**Option**\n\n| a | b |\n| --- | --- |\n| x | y |' }], answer_key: 'SECRET_KEY', rubric: 'SECRET_RUBRIC' };
const diagnostic = id => ({ assessment_id:'a1', status:'in_progress', current_question:id ? {...question,id}:null, progress:{answered:id==='q1'?0:1,target:5} });
const baseApi = () => ({createAssessment:async()=>({assessment_id:'a1',run_id:'r1'}),waitRun:async()=>{},assessment:async()=>({id:'a1',space_id:'s1',adaptive:true,status:'ready'}),diagnostic:async()=>diagnostic('q1')});

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');

test('independent round ignores stale local feedback and advances without entering practice feedback', async () => {
 const api = baseApi(), storage = memory(); let answered = false; const calls = [];
 storage.setItem('knowpath-learning-assessments-v1', JSON.stringify({s1:{assessmentId:'a1',answerFeedback:{question_id:'q0',explanation:'STALE_PARSE'}}}));
 api.assessment = async () => ({id:'a1',space_id:'s1',kind:'retest',feedback_policy:'after_round',status:'in_progress',question_count:5,
   questions:[question,{...question,id:'q2'}],answers:answered?[{question_id:'q1',answer:'A'}]:[],question_feedback:[]});
 api.recordAttempt = async (_id, body, options) => {calls.push({body,key:options.key});answered=true;return {question_feedback:[],next_question_id:'q2'};};
 const controller=createLearningController({api,context,storage});
 assert.equal(controller.snapshot().answerFeedback,null, 'local feedback needs server confirmation before display');
 await controller.resume();
 assert.equal(controller.snapshot().answerFeedback,null);
 await controller.answer('A');
 assert.equal(calls[0].body.include_feedback,false);
 assert.equal(controller.snapshot().diagnostic.current_question.id,'q2');
 assert.equal(controller.snapshot().answerFeedback,null);
 assert.equal(controller.snapshot().pendingAnswer,null);
 controller.dispose();
});

test('unknown answer response retains its originally sent body and key when the server feedback policy changes', async () => {
 const api=baseApi(),storage=memory(),calls=[];let policy='after_each_answer';
 api.assessment=async()=>({id:'a1',space_id:'s1',kind:'retest',feedback_policy:policy,status:'in_progress',question_count:5,questions:[question],answers:[],question_feedback:[]});
 api.recordAttempt=async(id,body,options)=>{calls.push({id,body:structuredClone(body),key:options.key});throw Error('response unknown');};
 const controller=createLearningController({api,context,storage});await controller.start();await controller.answer('A');controller.dispose();
 policy='after_round';const restored=createLearningController({api,context,storage});await restored.resume();await restored.answer('A');
 assert.equal(calls.length,2);assert.deepEqual(calls[1],calls[0]);assert.equal(calls[0].body.include_feedback,true);restored.dispose();
});

test('result explains unverified counts, configurable mastery gaps and actual retest availability', () => {
 const result = {question_results:[{question_id:'q1',verdict:'unverified',score:null,reference_answer:'参考定义'}],
   summary:{correct:0,incorrect:0,unverified:1,independent_evidence_count:0},current_state:{items:[{topic_id:'t1',status:'learning',
     evidence_sufficiency:{missing:['insufficient_independent_families','insufficient_distinct_assessments','insufficient_observation_spacing','no_application_observation'],
       independent_families:1,distinct_assessments:1,spacing_hours:0,requirements:{minimum_families:4,minimum_assessments:3,minimum_spacing_hours:48},
       earliest_retest_at:'2026-10-20T10:00:00Z',can_retest_now:false}}]}};
 const data={space:{id:'s1'},topics:[{id:'t1',name:'函数'}],assessment:{kind:'retest',questions:[question],answers:[{question_id:'q1',answer:'A'}]},result};
 const html=renderWorkspace({route:{page:'assessment-result',id:'a1'},data});
 assert.match(html,/独立复测/); assert.match(html,/待核验[^<]*1/);assert.match(html,/有效独立证据[^<]*0/);
 assert.match(html,/4.*题族/);assert.match(html,/3.*轮/);assert.match(html,/48.*小时/);assert.match(html,/应用题/);
 assert.match(html,/参考定义/);assert.match(html,/尚未到/);assert.doesNotMatch(html,/独立复测这个主题/);
 assert.match(html,/待核验 · 暂不自动判分/);assert.doesNotMatch(html,/未验证 · 未验证/);
});

test('assistance and retest times display full local date and minutes with truthful missing and invalid fallbacks', () => {
 const events=renderAssistanceReasons([{kind:'source_read',delivered_at:'2026-10-19T10:15:00Z'},{kind:'source_read',delivered_at:'2026-10-19T10:42:00Z'},
   {kind:'request_hint',delivered_at:'2026-10-19T10:42:00Z'},{kind:'message',delivered_at:null},{kind:'message',delivered_at:'invalid'}]);
 assert.match(events,/2026/);assert.match(events,/\d{2}:15/);assert.match(events,/\d{2}:42/);assert.match(events,/本地时间/);
 assert.match(events,/时间未记录/);assert.match(events,/时间无效/);
 assert.match(events,/请求提示/);assert.doesNotMatch(events,/request_hint/);
 const gaps=renderMasteryGaps([{topic_id:'t',evidence_sufficiency:{missing:['insufficient_observation_spacing'],requirements:{minimum_spacing_hours:24},
   earliest_retest_at:'2026-10-20T10:15:00Z',can_retest_now:false}}]);
 assert.match(gaps,/2026/);assert.match(gaps,/\d{2}:15/);assert.match(gaps,/本地时间/);
});

test('answer input and change events persist editable drafts without rendering', async () => {
 const handlers = new Map(), writes = [];
 const control = { name: 'answer', type: 'textarea', value: 'unfinished explanation', id: 'diagnostic-answer', dataset: {}, form: { id: 'learning-answer-form', dataset: { answerDraftKey: 'exact-question' } }, matches: () => false, closest: () => null };
 const context = vm.createContext({ document: { addEventListener: (name, handler) => handlers.set(name, handler) }, learning: { draftAnswer: (...args) => writes.push(args) }, render: () => assert.fail('editing must not render') });
 const start = app.indexOf("document.addEventListener('input'");
 vm.runInContext(app.slice(start, app.indexOf("document.addEventListener('keydown'", start)), context);
 await handlers.get('input')({ target: control }); await handlers.get('change')({ target: control });
 assert.deepEqual(writes, [['unfinished explanation', 'exact-question'], ['unfinished explanation', 'exact-question']]);
});

test('a necessary answer rerender restores focus, caret and scroll only for the unchanged question', () => {
 const field = { id: 'diagnostic-answer', selectionStart: 4, selectionEnd: 9, selectionDirection: 'backward', scrollTop: 80, focus(options) { this.focusOptions = options; }, setSelectionRange(...range) { this.restoredRange = range; }, matches: () => false };
 const form = { id: 'learning-answer-form', dataset: { answerDraftKey: 'a1:q1:0' }, elements: [field] };
 field.form = form;
 const document = { activeElement: field };
 const context = vm.createContext({ document, $: selector => selector === '#learning-answer-form' ? form : null });
 const start = app.indexOf('function captureAssessmentAnswer(');
 if (start >= 0) vm.runInContext(app.slice(start, app.indexOf('\nfunction render()', start)), context);
 assert.equal(typeof context.captureAssessmentAnswer, 'function', 'answer focus must have a render preservation path');
 const focus = context.captureAssessmentAnswer();
 field.selectionStart = field.selectionEnd = 0; field.scrollTop = 0;
 assert.equal(context.restoreAssessmentAnswer(focus), true);
 assert.deepEqual([...field.restoredRange], [4, 9, 'backward']); assert.equal(field.scrollTop, 80); assert.equal(field.focusOptions.preventScroll, true);
 form.dataset.answerDraftKey = 'a1:q2:0'; field.focusOptions = null;
 assert.equal(context.restoreAssessmentAnswer(focus), false); assert.equal(field.focusOptions, null);
});

function answerRenderFixture({ realHeader = false } = {}) {
 const listeners = new Map();
 const api = baseApi(), storage = memory(), document = { body: {}, documentElement: {}, addEventListener: (name, handler) => listeners.set(name, handler) };
 document.activeElement = document.body;
 api.diagnostic = async () => ({ ...diagnostic('q1'), current_question: { id: 'q1', type: 'short_answer', prompt: 'Explain' } });
 let form = null, controller;
 const main = { set innerHTML(html) {
   if (document.activeElement?.form?.id === 'learning-answer-form') document.activeElement = document.body;
   if (!html.includes('id="learning-answer-form"')) { form = null; return; }
   const view = controller.snapshot();
   const field = { id: 'diagnostic-answer', selectionStart: 0, selectionEnd: 0, selectionDirection: 'none', scrollTop: 0,
     matches: () => Boolean(view.busy || view.pendingAnswer), focus() { document.activeElement = this; },
     setSelectionRange(start, end, direction) { this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction; } };
   form = { id: 'learning-answer-form', dataset: { answerDraftKey: view.answerDraftKey }, elements: [field] }; field.form = form;
 } };
 const header = { set innerHTML(_) { if (document.activeElement?.inHeader) document.activeElement = document.body; } };
 const footer = { set innerHTML(_) {} };
 const noOp = () => '', scope = vm.createContext({ document, main, location: { hash: '#/learning/diagnostic' },
   state: { loading: false, error: '', selected: '' }, renderVersion: 0, assessmentAnswerFocus: null,
   route: () => ({ name: 'learning', id: 'diagnostic' }), currentSpace: () => ({ id: 's1' }),
   $: selector => selector === '#learning-answer-form' ? form : selector === '#header' ? header : selector === '#footer' ? footer : null,
   header: noOp, legacySpaceRedirect: noOp, overview: noOp, spacesPage: noOp, materialsPage: noOp,
   graphPage: noOp, planPage: noOp, assistantPage: noOp, managedPage: noOp,
   spacePage: () => renderLearning({ space: { id: 's1' }, state: controller.snapshot() }),
   captureStudySidebarScroll: noOp, subbar: noOp, syncAssessmentTicker: noOp,
   updateProgressPosition: noOp, updateStudySidebar: noOp, routes: [], logo: '', button: noOp, manageLink: noOp,
 });
 const start = app.indexOf('function captureAssessmentAnswer(');
 vm.runInContext(app.slice(start, app.indexOf('const pendingMaterialStatuses', start)), scope);
 const pointerStart = app.indexOf("document.addEventListener('pointerdown'");
 if (pointerStart >= 0) vm.runInContext(app.slice(pointerStart, app.indexOf("document.addEventListener('submit'", pointerStart)), scope);
 if (realHeader) vm.runInContext(app.slice(app.indexOf('function header()'), app.indexOf('\nfunction subbar()')), scope);
 controller = createLearningController({ api, storage, context, changed: () => scope.render() }); scope.learning = controller;
 return { api, controller, document, field: () => form?.elements[0], pointerDown: target => listeners.get('pointerdown')?.({ target }) };
}

test('refreshing an unchanged question restores its draft focus and caret after intermediate disabled renders', async () => {
 const fixture = answerRenderFixture(); await fixture.controller.start();
 fixture.controller.draftAnswer('an unfinished explanation');
 const before = fixture.field(); before.focus(); before.setSelectionRange(3, 8, 'backward'); before.scrollTop = 60;
 await fixture.controller.resume();
 const after = fixture.field(); assert.notEqual(before, after);
 assert.equal(fixture.document.activeElement, after, 'busy renders must defer focus restoration until the same question is enabled');
 assert.equal(after.selectionStart, 3); assert.equal(after.selectionEnd, 8); assert.equal(after.selectionDirection, 'backward'); assert.equal(after.scrollTop, 60);
 assert.equal(fixture.controller.snapshot().answerDraft.answer, 'an unfinished explanation'); fixture.controller.dispose();
});

test('refresh never steals focus from a control selected while the answer is disabled', async () => {
 const fixture = answerRenderFixture(); await fixture.controller.start();
 fixture.field().focus(); fixture.field().setSelectionRange(2, 7, 'forward');
 let resolve; fixture.api.assessment = () => new Promise(done => { resolve = done; });
 const pending = fixture.controller.resume();
 const otherControl = { id: 'outside-answer-control' }; fixture.document.activeElement = otherControl;
 resolve({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }); await pending;
 assert.equal(fixture.document.activeElement, otherControl); fixture.controller.dispose();
});

test('header replacement during refresh cannot recapture focus from an external control', async () => {
 const fixture = answerRenderFixture({ realHeader: true }); await fixture.controller.start();
 fixture.field().focus(); fixture.field().setSelectionRange(2, 7, 'forward');
 let resolve; fixture.api.assessment = () => new Promise(done => { resolve = done; });
 const pending = fixture.controller.resume();
 fixture.document.activeElement = { id: 'header-search', inHeader: true };
 resolve({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }); await pending;
 assert.equal(fixture.document.activeElement, fixture.document.body, 'capture the external focus before header removes that control');
 fixture.controller.dispose();
});

test('a successful answer focus restoration is consumed before the user moves focus to body', async () => {
 const fixture = answerRenderFixture(); await fixture.controller.start();
 fixture.field().focus(); fixture.field().setSelectionRange(1, 6, 'forward');
 await fixture.controller.resume(); assert.equal(fixture.document.activeElement, fixture.field());
 fixture.document.activeElement = fixture.document.body;
 await fixture.controller.resume();
 assert.equal(fixture.document.activeElement, fixture.document.body, 'a completed restoration cannot remain as a reason to reclaim focus');
 fixture.controller.dispose();
});

test('a pointer interaction on blank content cancels deferred answer focus while refresh is pending', async () => {
 const fixture = answerRenderFixture(); await fixture.controller.start();
 fixture.field().focus(); fixture.field().setSelectionRange(1, 6, 'forward');
 let resolve; fixture.api.assessment = () => new Promise(done => { resolve = done; });
 const pending = fixture.controller.resume();
 assert.equal(fixture.document.activeElement, fixture.document.body);
 fixture.pointerDown(fixture.document.body);
 resolve({ id: 'a1', space_id: 's1', adaptive: true, status: 'ready' }); await pending;
 assert.equal(fixture.document.activeElement, fixture.document.body, 'a deliberate pointer interaction abandons the deferred restoration');
 fixture.controller.dispose();
});

test('accepted answer pauses on durable feedback until explicit next', async () => {
 const storage=memory(),api=baseApi(); let answered=false,posts=0;
 const feedback={question_id:'q1',verdict:'incorrect',answer:'A',correct_answer:'B',explanation:'**Reason**: source definition',source_refs:[{page:2}]};
 api.recordAttempt=async(_id,body)=>{posts++;assert.equal(body.include_feedback,true);answered=true;return {question_feedback:[feedback]};};
 api.assessment=async()=>({id:'a1',space_id:'s1',adaptive:true,status:'in_progress',question_feedback:answered?[feedback]:[]});
 api.diagnostic=async()=>diagnostic(answered?'q2':'q1');
 const controller=createLearningController({api,context,storage});await controller.start();await controller.answer('A');
 assert.equal(controller.snapshot().answerFeedback.question_id,'q1');
 const html=renderLearning({space:{id:'s1'},state:controller.snapshot()});
 assert.match(html,/回答错误/);assert.match(html,/<strong>Reason<\/strong>/);assert.match(html,/learning-next/);assert.doesNotMatch(html,/id="learning-answer-form"/);
 const restored=createLearningController({api,context,storage});await restored.resume();assert.equal(restored.snapshot().answerFeedback.question_id,'q1');
 await restored.next();assert.equal(restored.snapshot().answerFeedback,null);assert.equal(restored.snapshot().diagnostic.current_question.id,'q2');assert.equal(posts,1);
 await restored.resume();assert.equal(restored.snapshot().answerFeedback,null);
});
test('completed assessment links to encoded chapter without crashing', () => {
 const html=renderLearning({space:{id:'space/1'},state:{assessmentId:'a1',diagnostic:{...diagnostic(null),status:'completed'},result:{question_results:[],notes:{chapter_id:'ch/1 &'}}}});
 assert.match(html, /href="#\/space\/space%2F1\/notes\?chapter=ch%2F1\+%26"/);
});
test('acknowledged attempt survives refresh failure without another evidence POST',async()=>{
 const storage=memory(),api=baseApi();let posts=0,fail=false;
 api.recordAttempt=async()=>{posts++;fail=true;return {status:'recorded'};};
 api.assessment=async()=>{if(fail)throw new Error('refresh offline');return {id:'a1',space_id:'s1',adaptive:true,status:'ready'};};
 const controller=createLearningController({api,context,storage});await controller.start();await controller.answer('A');
 assert.equal(controller.snapshot().pendingAnswer?.acknowledged,true);
 assert.match(controller.snapshot().error,/已记录/);
 const html=renderLearning({space:{id:'s1'},state:controller.snapshot()});assert.doesNotMatch(html,/id="learning-answer-form"/);
 await controller.answer('A');assert.equal(posts,1);
 fail=false;api.diagnostic=async()=>diagnostic('q2');
 const restored=createLearningController({api,context,storage});await restored.resume();assert.equal(restored.snapshot().diagnostic.current_question.id,'q2');assert.equal(posts,1);
});
test('acknowledged finalization resumes same run after polling failure',async()=>{
 const storage=memory(),api=baseApi();let posts=0,fail=false,polls=[];
 api.diagnostic=async()=>diagnostic(null);api.finalizeAssessment=async()=>{posts++;return {run_id:'final1'};};
 api.waitRun=async id=>{polls.push(id);if(id==='final1'&&fail)throw new Error('poll offline');};
 const controller=createLearningController({api,context,storage});await controller.start();fail=true;await controller.finalize();
 assert.equal(controller.snapshot().pendingFinalize?.acknowledged,true);assert.match(controller.snapshot().error,/已接收/);
 await controller.finalize();assert.equal(posts,1);
 fail=false;api.assessment=async()=>({id:'a1',space_id:'s1',adaptive:true,status:'completed'});api.diagnostic=async()=>({...diagnostic(null),status:'completed'});api.assessmentResult=async()=>({question_results:[]});
 const restored=createLearningController({api,context,storage});await restored.resume();assert.equal(restored.snapshot().result.question_results.length,0);assert.equal(posts,1);assert.ok(polls.filter(id=>id==='final1').length>=2);
});
test('assessment Markdown uses block containers, safe links and accessible whole option rows',()=>{
 const html=renderLearning({space:{id:'s1'},state:{assessmentId:'a1',diagnostic:{...diagnostic('q1'),current_question:{...question,prompt:question.prompt+'\n\n<script>alert(1)</script> [bad](javascript:alert)'}}}});
 assert.match(html,/<strong>Prompt<\/strong>/);assert.match(html,/<pre><code class="language-js">/);assert.match(html,/<table>/);
 assert.match(html,/aria-labelledby="diagnostic-question-label"/);assert.match(html,/for="diagnostic-option-0"/);assert.match(html,/aria-labelledby="diagnostic-option-text-0"/);
 assert.doesNotMatch(html,/<script>|href="javascript:|SECRET_KEY|SECRET_RUBRIC|<legend><p>|<span><p>/);
});
test('result and review explanations render safe Markdown in blocks',()=>{
 const data={space:{id:'s1'},assessment:{questions:[question],answers:[{question_id:'q1',answer:'A'}]},result:{question_results:[{question_id:'q1',verdict:'correct',feedback:'**Feedback**\n\n- reason'}]},reviews:{items:[{review_id:'review',question,status:'completed',events:[{action:'reject',reason:'**Explanation**\n\n```txt\nproof\n```'}]}]}};
 const result=renderWorkspace({route:{page:'assessment-result',id:'a1'},data});assert.match(result,/<strong>Prompt<\/strong>/);assert.match(result,/<strong>Feedback<\/strong>/);assert.match(result,/<table>/);assert.doesNotMatch(result,/<h3>1\. <p>/);
 const review=renderWorkspace({route:{page:'assessment-reviews',id:'a1'},data});assert.match(review,/<strong>Explanation<\/strong>/);assert.match(review,/<pre><code class="language-txt">proof/);
});
test('generation shows persisted genuine stages and elapsed time, retaining them after timeout',async()=>{
 const storage=memory(),api=baseApi();let phase='validating';
 api.waitRun=async(id,signal,onProgress)=>{assert.equal(typeof onProgress,'function');onProgress({assessment_stage:'queued',created_at:'2026-10-10T00:00:00Z'});onProgress({assessment_stage:'generating_questions',created_at:'2026-10-10T00:00:00Z'});onProgress({assessment_stage:phase === 'validating' ? 'validating_questions' : phase,created_at:'2026-10-10T00:00:00Z'});throw new Error('connection lost');};
 const controller=createLearningController({api,context,storage});await controller.start();assert.equal(controller.snapshot().assessmentStage,'validating');assert.ok(controller.snapshot().generationStartedAt);
 const html=renderLearning({space:{id:'s1'},state:controller.snapshot()});assert.match(html,/验证题目/);assert.match(html,/已等待/);assert.doesNotMatch(html,/题目已就绪|\d+%/);
 const restored=createLearningController({api,context,storage});assert.equal(restored.snapshot().assessmentStage,'validating');assert.equal(restored.snapshot().generationStartedAt,controller.snapshot().generationStartedAt);
 for(const [stage,text] of [['waiting','等待出题'],['generating','生成题目'],['validating','验证题目'],['ready','题目已就绪']])assert.ok(renderLearning({space:{id:'s1'},state:{assessmentId:'a1',assessmentStatus:'generating',assessmentStage:stage,generationStartedAt:Date.now()-5000}}).includes(text));
});
test('unknown finalization response retries the same durable body and key after reload',async()=>{
 const storage=memory(),api=baseApi(),calls=[];api.diagnostic=async()=>diagnostic(null);
 api.finalizeAssessment=async(id,body,options)=>{calls.push({id,body,key:options.key});throw new Error('response unknown');};
 const controller=createLearningController({api,context,storage});await controller.start();await controller.finalize();assert.equal(controller.snapshot().pendingFinalize.acknowledged,false);
 const restored=createLearningController({api,context,storage});await restored.resume();await restored.finalize();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
});
test('late acknowledged submission is durable for its original space and cannot leak into another',async()=>{
 let selected='s1',resolve;const storage=memory(),api=baseApi();api.recordAttempt=()=>new Promise(done=>{resolve=done;});
 const controller=createLearningController({api,storage,context:()=>({spaceId:selected,active:true})});await controller.start();const pending=controller.answer('A');selected='s2';controller.sync();resolve({status:'recorded'});await pending;
 assert.equal(controller.snapshot().pendingAnswer,null);assert.equal(controller.snapshot().assessmentId,'');
 selected='s1';assert.equal(controller.snapshot().pendingAnswer.acknowledged,true);
});
