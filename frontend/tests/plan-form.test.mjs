import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { renderScheduleSummary } from '../src/progress.js';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const source = app.slice(app.indexOf('async function createPlan(form)'), app.indexOf('\nasync function completeTask'));

test('actual rolling plan form sends the strict backend calendar timezone field', async () => {
  let request;
  const values = new Map([['schedule_mode', 'budget_rolling'], ['include_review', 'on'],
    ['daily_minutes', '15'], ['horizon_days', '7'], ['calendar_timezone', 'UTC'], ['timezone', 'UTC']]);
  const fn = new Function('byId', 'state', 'FormData', 'planSettings', '$', 'api', 'modalVersion', 'routeVersion', 'location', 'modal', 'currentSpace', source + '; return createPlan;')(
    () => ({ id: 'space' }), { plans: new Map() }, class { constructor() { return values; } },
    () => ({ sessions: { valid: true, value: 3 }, minutes: { valid: true, value: 25 } }),
    () => null, { createPlan: async (_, payload) => { request = payload; throw Error('CAPTURED'); } },
    1, 0, { hash: '#/space/space/plan' }, { open: true }, () => ({ id: 'space' }));
  let aborted = false;
  const form = { isConnected: true, dataset: { space: 'space' }, querySelectorAll: () => [], previewSignature: 'rendered-settings', previewRevision: 3, previewController: {abort: () => {aborted = true;}} };
  await assert.rejects(fn(form), /CAPTURED/);
  assert.equal(form.previewSignature, null, 'a failed submission must allow the same settings to preview again');
  assert.equal(form.previewRevision, 4, 'creation invalidates any pending read-only preview');
  assert.equal(aborted, true);
  assert.equal(request.calendar_timezone, 'UTC');
  assert.equal(request.daily_minutes, 15);
  assert.equal(request.horizon_days, 7);
  assert.ok(!('timezone' in request));
  assert.ok(!('session_count' in request));
});

test('preview gives four separate real totals and marks forecasts as estimates', () => {
  const html = renderScheduleSummary({selected_minutes: 420, capacity_minutes: 210, allocated_minutes: 210, unallocated_minutes: 210, unallocated_count: 11});
  assert.match(html, /总工作量（估计）.*420 分钟/);
  assert.match(html, /窗口容量.*210 分钟/);
  assert.match(html, /已安排（估计）.*210 分钟/);
  assert.match(html, /未安排（估计）.*210 分钟 \/ 11 个节点/);
  assert.match(html, /预览和阅读/);
});

test('late and closed-modal preview responses never replace the latest budget', async () => {
  const start = app.indexOf('async function loadPlanPreview('), end = app.indexOf('\nfunction planBudgetActions', start);
  const pending = [], target = {innerHTML: '', textContent: ''}, modal = {open: true};
  const fn = new Function('AbortController', 'modalVersion', 'modal', 'state', 'api', '$', 'renderScheduleSummary', 'planBudgetActions', app.slice(start, end) + '; return loadPlanPreview;')(
    AbortController, 1, modal, {plans: new Map()}, {previewPlan: (_, body) => new Promise(resolve => pending.push({body, resolve}))}, () => target, renderScheduleSummary, () => 'ACTIONS');
  const form = {isConnected: true, dataset: {space: 's'}, previewRevision: 1, querySelectorAll: () => [], elements: {
    daily_minutes: {value: '30'}, horizon_days: {value: '7'}, calendar_timezone: {value: 'UTC'}, include_review: {checked: true}, order_mode: {value: 'source'}}};
  const first = fn(form, 1); form.previewRevision = 2; form.elements.daily_minutes.value = '60'; const second = fn(form, 2);
  pending[1].resolve({summary: {selected_minutes: 420, capacity_minutes: 420, allocated_minutes: 420, unallocated_minutes: 0, unallocated_count: 0}}); await second;
  const latest = target.innerHTML;
  pending[0].resolve({summary: {selected_minutes: 420, capacity_minutes: 210}}); await first;
  assert.equal(target.innerHTML, latest);
  assert.equal(pending[1].body.daily_minutes, 60);
  form.previewRevision = 3; const closed = fn(form, 3); modal.open = false;
  pending[2].resolve({summary: {selected_minutes: 999}}); await closed;
  assert.equal(target.innerHTML, latest);
});

test('input followed by blur change preserves preview action nodes for the first click', () => {
  const source = app.slice(app.indexOf('function updatePlanPreview('), app.indexOf('\nasync function loadPlanPreview('));
  let replacements = 0, requests = 0;
  const preview = {set textContent(value) { replacements++; }, innerHTML: ''}, submit = {};
  const fields = Object.fromEntries(['schedule_mode', 'daily_minutes', 'horizon_days', 'calendar_timezone', 'order_mode'].map((name, i) => [name, {value: ['budget_rolling', '60', '7', 'UTC', 'source'][i]}]));
  fields.include_review = {checked: true};
  const form = {dataset: {space: 's'}, elements: fields, querySelector: () => ({}), querySelectorAll: () => []};
  const update = new Function('$', 'loadPlanPreview', 'state', source + '; return updatePlanPreview;')(
    selector => selector === '#plan-preview' ? preview : submit, () => requests++, {plans: new Map()});
  update(form); // input: load the new budget
  preview.innerHTML = '<button data-action="plan-preview-scope">缩小学习范围</button>';
  const writes = replacements, revision = form.previewRevision;
  update(form); // mousedown on that button blurs the input and dispatches change
  assert.equal(replacements, writes, 'blur must not detach the mousedown target before click');
  assert.equal(form.previewRevision, revision);
  assert.equal(requests, 1);
  fields.horizon_days.value = '14'; update(form);
  assert.equal(requests, 2, 'a real budget change still gets a fresh preview');
});

test('scope subflow restores every actual plan choice only in its owning space', () => {
  const start = app.indexOf('function capturePlanDraft('), end = app.indexOf('\nasync function returnToPlanDraft(', start);
  assert.ok(start >= 0 && end > start, 'plan scope subflow must preserve an ephemeral complete draft');
  const {capturePlanDraft, restorePlanDraft} = new Function(app.slice(start, end) + '; return {capturePlanDraft, restorePlanDraft};')();
  const fields = Object.entries({schedule_mode:'budget_rolling',daily_minutes:'60',horizon_days:'14',calendar_timezone:'UTC',order_mode:'adaptive',session_count:'custom',session_count_custom:'4',minutes_per_session:'custom',minutes_per_session_custom:'55',include_review:'on'}).map(([name,value])=>({name,value,type:name==='include_review'?'checkbox':'text',checked:false}));
  let rows = ['m2','m1'].map(id=>({dataset:{newPlanMaterial:id}}));
  const parent = {append(row) { rows = rows.filter(r=>r!==row); rows.push(row); }}; rows.forEach(row=>row.parentElement=parent);
  const form = {dataset:{space:'s'},elements:Object.fromEntries(fields.map(field=>[field.name,field])),querySelectorAll: selector=>selector.includes('data-new-plan-material')?rows:fields};
  const draft = capturePlanDraft(form);
  fields.forEach(field=>{field.value='reset';field.checked=true;}); rows.reverse();
  assert.equal(restorePlanDraft(form,draft),true);
  assert.equal(form.elements.daily_minutes.value,'60'); assert.equal(form.elements.horizon_days.value,'14');
  assert.equal(form.elements.calendar_timezone.value,'UTC'); assert.equal(form.elements.order_mode.value,'adaptive');
  assert.equal(form.elements.include_review.checked,false); assert.equal(form.elements.session_count_custom.value,'4');
  assert.equal(form.elements.minutes_per_session_custom.value,'55');
  assert.deepEqual(rows.map(row=>row.dataset.newPlanMaterial),['m2','m1']);
  form.dataset.space='other'; form.elements.daily_minutes.value='12';
  assert.equal(restorePlanDraft(form,draft),false); assert.equal(form.elements.daily_minutes.value,'12');
});

test('return from scope reloads the current base version and ignores replaced or foreign-space callbacks', async () => {
  const source = app.slice(app.indexOf('async function returnToPlanDraft('), app.indexOf('\nasync function cancelModal('));
  const draft = {spaceId: 's', fields: [], materialOrder: []};
  for (const interrupt of ['none', 'space', 'modal', 'during-workbench']) {
    let current = 's', resolveSpace, resolveWorkbench, opened = 0, restored = 0, baseVersion = 1;
    const space = {id:'s'}, pending = new Promise(resolve => { resolveSpace = resolve; });
    const workbenchPending = new Promise(resolve => { resolveWorkbench = resolve; });
    const harness = new Function('currentSpace', 'api', 'byId', 'mergeSpaceProjection', 'workbench', 'planModal', `
      let modalVersion = 0; const modal = {open:true};
      function openModal() { modalVersion++; }
      ${source}
      return {run:returnToPlanDraft, replace:()=>modalVersion++};`)(
      () => ({id:current}), {space:()=>pending}, () => space, (_, latest) => latest,
      {invalidate:()=>{}, load:async()=>{opened++; await workbenchPending; baseVersion=9;}, snapshot:()=>({data:{current_plan:{version:baseVersion}}})},
      value => {assert.equal(value,draft); assert.equal(baseVersion,9); restored++;});
    const returning = harness.run(draft);
    if (interrupt === 'space') current = 'other';
    if (interrupt === 'modal') harness.replace();
    resolveSpace({id:'s', topic_ids:['fresh-topic']});
    await Promise.resolve(); await Promise.resolve();
    if (interrupt === 'during-workbench') current = 'other';
    resolveWorkbench(); await returning;
    assert.equal(restored, interrupt === 'none' ? 1 : 0, interrupt);
    assert.equal(opened, ['none','during-workbench'].includes(interrupt) ? 1 : 0, interrupt);
  }
});

test('a scope save finishing after navigation cannot close or reopen the new modal', async () => {
  const source = app.slice(app.indexOf('async function saveScopeForm('), app.indexOf('\nasync function scopeModal('));
  for (const interrupt of ['space', 'modal', 'detached']) {
    let finish, closed = 0, returned = 0, cleared = 0, current = 's';
    const space = {id:'s'}, draft = {spaceId:'s'}, form = {dataset:{space:'s',returnPlan:'true'},isConnected:true};
    const harness = new Function('byId','saveScope','scopePayload','FormData','mergeSpaceProjection','state','workbench','currentSpace','learning','closeModal','returnToPlanDraft','toast', `
      let modalVersion=1, scopePlanReturn={modalVersion:1,draft:{spaceId:'s'}};
      ${source}
      return {run:saveScopeForm, replace:()=>modalVersion++};`)(
      () => space, () => new Promise(resolve=>{finish=resolve;}), () => ({}), class {}, (_,result)=>result,
      {plans:new Map([['s',{}]])}, {invalidate:()=>{}}, () => ({id:current}), {clear:()=>cleared++},
      () => closed++, () => returned++, () => {});
    const saving = harness.run(form);
    if (interrupt === 'space') current='other';
    if (interrupt === 'modal') harness.replace();
    if (interrupt === 'detached') form.isConnected=false;
    finish({id:'s',scope_version:2}); await saving;
    assert.equal(space.scope_version,2,'committed projection is retained');
    assert.equal(closed,0,interrupt); assert.equal(returned,0,interrupt); assert.equal(cleared,0,interrupt);
  }
});

test('scope cancel and ESC return the draft without a scope write', async () => {
  const source = app.slice(app.indexOf('async function cancelModal('), app.indexOf('\nasync function saveScopeForm('));
  const draft = {spaceId:'s'}, returned=[];
  const cancel = new Function('currentSpace','returnToPlanDraft', `
    let modalVersion=3, scopePlanReturn={modalVersion:3,draft:{spaceId:'s'}};
    function closeModal() { scopePlanReturn=null; modalVersion++; }
    ${source}
    return cancelModal;`)(() => ({id:'s'}), value=>returned.push(value));
  await cancel(); await cancel();
  assert.deepEqual(returned,[draft]);
  assert.match(app, /'close-modal': cancelModal/);
  assert.match(app, /addEventListener\('cancel',[^\n]*cancelModal\(\)/);
  assert.match(app, /addEventListener\('click',[^\n]*cancelModal\(\)/);
});
