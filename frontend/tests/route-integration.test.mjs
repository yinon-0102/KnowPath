import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { nodeSessionState } from '../src/progress.js';
const app=await readFile(new URL('../src/app.js',import.meta.url),'utf8');
const extract=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
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
