import test from 'node:test';
import assert from 'node:assert/strict';
import { createPlanOrderController, renderPlanOrder, selectAssessmentOption, assessmentStageText } from '../src/plan-order.js';
const plan = { id: 'p', version: 2, config: {}, tasks: [{ id:'a', node_id:'a', status:'completed' },{ id:'b',node_id:'b',status:'pending' },{ id:'c',node_id:'c',status:'pending' }] };
const space = { bindings: [{material_id:'m1'},{material_id:'m2'}] };

test('prerequisite conflict shows readable reason and feasible order without replacing custom draft', () => {
 const controller = createPlanOrderController({ api: {} }); controller.open(plan, space); controller.configure({ order_mode: 'custom' });
 const state = controller.snapshot(); state.preview = { can_apply: false, candidate_plan: plan, conflicts: [{ message: '先学习变量再学习函数' }], suggested_plan: { tasks: [{ title: '变量' }, { title: '函数' }] } };
 const before = [...state.ordered_node_ids]; const html = renderPlanOrder(state);
 assert.match(html, /先学习变量再学习函数/); assert.match(html, /建议可行顺序：变量 → 函数/); assert.deepEqual(state.ordered_node_ids, before);
 assert.match(html, /data-action="order-apply" disabled/);
});
test('custom ordering sends only movable stable ids and fixed tasks cannot move', async () => {
  let body;
  const controller = createPlanOrderController({ api:{ orderPreview:async(id,payload)=> {body=payload; return {plan_id:id,expected_plan_version:2,can_apply:true,fixed_nodes:[{node_id:'a'}],candidate_plan:plan};} } });
  controller.open(plan,space); controller.configure({order_mode:'custom'}); controller.move('node','c',-1);
  assert.deepEqual(controller.snapshot().ordered_node_ids,['c','b']);
  controller.move('node','a',1); await controller.preview();
  assert.deepEqual(body.ordered_node_ids,['c','b']); assert.equal(body.expected_plan_version,2);
});
test('apply requires current preview and retries preserve idempotency key and draft', async () => {
  const calls=[]; let attempts=0;
  const controller=createPlanOrderController({api:{orderPreview:async()=>({plan_id:'p',expected_plan_version:2,can_apply:true,candidate_plan:plan,fixed_nodes:[]}),reorderPlan:async(id,body,options)=>{calls.push([body,options.key]); if(!attempts++) throw new Error('timeout'); return {...plan,version:3};}},uid:()=> 'same-key'});
  controller.open(plan,space); await assert.rejects(controller.apply(),/预览/);
  await controller.preview(); await assert.rejects(controller.apply(),/timeout/); await controller.apply();
  assert.equal(calls[0][1],calls[1][1]); assert.deepEqual(calls[0][0],calls[1][0]);
});
test('changes invalidate preview; conflict preserves chosen material order',async()=>{
  const controller=createPlanOrderController({api:{orderPreview:async()=>({plan_id:'p',expected_plan_version:2,can_apply:false,conflicts:[{message:'conflict'}]})}});
  controller.open(plan,space); controller.move('material','m2',-1); await controller.preview();
  assert.deepEqual(controller.snapshot().material_order,['m2','m1']); await assert.rejects(controller.apply(),/预览/);
  controller.configure({order_mode:'adaptive'}); assert.equal(controller.snapshot().preview,null);
});
test('whole option selection respects links, controls and disabled fieldsets',()=>{
  let dispatched=0; const radio={checked:false,matches:()=>false,dispatchEvent:()=>dispatched++};
  const row={querySelector:()=>radio}; const target={closest:()=>null};
  assert.equal(selectAssessmentOption(row,target,()=>({})),true); assert.equal(radio.checked,true); assert.equal(dispatched,1);
  radio.matches=()=>true; assert.equal(selectAssessmentOption(row,target),false);
  radio.matches=()=>false; assert.equal(selectAssessmentOption(row,{closest:()=>({})}),false);
});
test('elapsed generation label keeps real stage and clamps future times',()=>{
 assert.equal(assessmentStageText('validating',1000,4100),'验证题目 · 已等待 3 秒');
 assert.equal(assessmentStageText('waiting',5000,1000),'等待出题 · 已等待 0 秒');
});
test('version conflict leaves unsaved order available and refresh keeps surviving choices',async()=>{
 const latest={...plan,version:4,tasks:[...plan.tasks,{id:'d',node_id:'d',status:'pending'}]};
 const controller=createPlanOrderController({api:{orderPreview:async()=>{throw Object.assign(new Error('changed'),{code:'VERSION_CONFLICT'});},plan:async()=>latest}});
 controller.open(plan,space);controller.configure({order_mode:'custom'});controller.move('node','c',-1);
 await assert.rejects(controller.preview(),/changed/);assert.deepEqual(controller.snapshot().ordered_node_ids,['c','b']);
 await controller.refresh(); assert.equal(controller.snapshot().plan.version,4);assert.deepEqual(controller.snapshot().ordered_node_ids,['c','b','d']); assert.equal(controller.snapshot().order_mode,'custom');
});
test('late previews cannot replace a reopened plan dialog',async()=>{
 let resolve; const controller=createPlanOrderController({api:{orderPreview:()=>new Promise(r=>resolve=r)}});
 controller.open(plan,space);const request=controller.preview();controller.open({...plan,id:'other'},space);
 resolve({plan_id:'p',expected_plan_version:2,can_apply:true});await request;
 assert.equal(controller.snapshot().plan.id,'other');assert.equal(controller.snapshot().preview,null);
});
test('paused current session is fixed before the first custom preview',()=>{
 const controller=createPlanOrderController({api:{}});controller.open(plan,space,{session_id:'s',plan_id:'p',task_id:'b',status:'active',paused:true});
 assert.deepEqual(controller.snapshot().ordered_node_ids,['c']);
});
