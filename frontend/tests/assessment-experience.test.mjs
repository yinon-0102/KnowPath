import test from 'node:test';
import assert from 'node:assert/strict';
import { createLearningController, renderLearning } from '../src/learning.js';
import { renderWorkspace } from '../src/workspace-view.js';
const memory = () => { const data = new Map(); return { getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v) }; };
const context = () => ({ spaceId: 's1', active: true });
const question = { id: 'q1', type: 'single_choice', prompt: '**Prompt**\n\n- item\n\n```js\nconst a = 1;\n```', options: [{ id:'A', text: '**Option**\n\n| a | b |\n| --- | --- |\n| x | y |' }], answer_key: 'SECRET_KEY', rubric: 'SECRET_RUBRIC' };
const diagnostic = id => ({ assessment_id:'a1', status:'in_progress', current_question:id ? {...question,id}:null, progress:{answered:id==='q1'?0:1,target:5} });
const baseApi = () => ({createAssessment:async()=>({assessment_id:'a1',run_id:'r1'}),waitRun:async()=>{},assessment:async()=>({id:'a1',space_id:'s1',adaptive:true,status:'ready'}),diagnostic:async()=>diagnostic('q1')});
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
