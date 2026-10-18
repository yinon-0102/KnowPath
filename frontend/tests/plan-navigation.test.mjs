import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const app = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const extract = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function harness(heldStage = 'detail') {
  const gate = deferred(), entered = deferred(), calls = [], saved = {};
  const space = { id: 's' }, plan = { id: 'p', version: 2, tasks: [] };
  const form = { id: 'plan-form', dataset: { space: 's' }, isConnected: true, querySelectorAll: () => [] };
  const errorBox = { hidden: true, textContent: '' };
  const modal = { open: true, close() { this.open = false; } };
  const submit = { form, disabled: false, setAttribute() {}, removeAttribute() {} };
  let hashchange;
  const stage = async (name, value) => {
    calls.push(name);
    if (name === heldStage) { entered.resolve(); await gate.promise; }
    return value;
  };
  const context = vm.createContext({
    modalVersion: 1, routeVersion: 0, scopePlanReturn: null, modal, form,
    location: { hash: '#/space/s/plan' }, state: { plans: new Map(), timerDuration: 99, selected: 's' },
    currentSpace: () => ({ id: context.state.selected }), byId: () => space,
    FormData: class { get(name) { return ({ schedule_mode: 'budget_rolling', daily_minutes: '30', horizon_days: '7', calendar_timezone: 'Asia/Shanghai' })[name]; } has() { return true; } },
    planSettings: () => ({}), $: selector => selector === '[data-form-error]' ? errorBox : null,
    api: { createPlan: () => stage('accept', { plan_id: 'p', run_id: 'run', status: 'pending' }), waitRun: () => stage('run'), plan: () => stage('detail', plan) },
    workspace: { invalidateSpace: id => calls.push(`workspace:${id}`), sync() {} },
    storageRead: () => saved, storageWrite: (_, key, value) => calls.push(`saved:${value.s}`), session: {},
    workbench: { invalidate: id => calls.push(`workbench:${id}`), load: () => stage('workbench-load') },
    resetTimer: () => calls.push('timer'), openSpaceTab: (tab, id) => calls.push(`navigate:${id}/${tab}`),
    toast: message => calls.push(`toast:${message}`), updatePlanPreview: () => calls.push('preview'),
    render() {}, main: { focus() {} }, loadRoute() {}, window: { addEventListener: (_, handler) => { hashchange = handler; }, scrollTo() {} },
  });
  vm.runInContext(extract('function closeModal()', '\nasync function busy('), context);
  vm.runInContext(extract('async function busy(', '\nfunction header('), context);
  vm.runInContext(extract('async function cancelModal(', '\nasync function saveScopeForm('), context);
  vm.runInContext(extract('async function createPlan(form)', '\nasync function continueAfterTaskConfirmation('), context);
  vm.runInContext(app.split('\n').find(line => line.startsWith("window.addEventListener('hashchange'")), context);
  return {
    calls, saved, form, modal, errorBox, context, gate, entered: entered.promise,
    run: () => context.busy(submit, () => context.createPlan(form)),
    interrupt(kind) {
      if (kind === 'close') context.closeModal();
      if (kind === 'escape') context.cancelModal();
      if (kind === 'space') context.state.selected = 'other';
      if (kind === 'new-modal') { context.modalVersion++; modal.open = true; }
      if (kind === 'new-form') form.isConnected = false;
      if (kind === 'route') { context.location.hash = '#/spaces'; hashchange(); }
      if (kind === 'route-return') {
        // Even a connected dialog/form cannot become current again on revisiting the hash.
        modal.open = false; context.location.hash = '#/spaces'; hashchange();
        context.location.hash = '#/space/s/plan'; hashchange(); modal.open = true;
      }
      if (kind === 'hash-before-event') context.location.hash = '#/spaces';
    },
  };
}

for (const stage of ['accept', 'run', 'detail', 'workbench-load']) {
  for (const interrupt of ['close', 'escape', 'space', 'new-modal', 'new-form', 'route', 'route-return', 'hash-before-event']) {
    test(`late accepted plan at ${stage} preserves user ${interrupt}`, async () => {
      const h = harness(stage), running = h.run(); await h.entered;
      h.interrupt(interrupt); const open = h.modal.open;
      h.gate.resolve(); await running;
      assert.equal(h.saved.s, 'p', 'accepted plan identity remains recoverable');
      assert.equal(h.context.state.plans.get('s').id, 'p', 'accepted plan data stays in its space');
      assert.ok(h.calls.includes('workspace:s'), 'accepted mutation invalidates managed task cache');
      assert.equal(h.context.state.timerDuration, 99, 'old submission must not change current timer');
      assert.equal(h.modal.open, open, 'old submission must not close a replacement dialog');
      assert.ok(!h.calls.some(value => /^(timer|navigate:|toast:)/.test(value)), h.calls.join(', '));
      assert.equal(h.errorBox.hidden, true);
    });
  }
}

for (const stage of ['accept', 'run', 'detail', 'workbench-load']) {
  test(`late ${stage} failure cannot write an error into a new form`, async () => {
    const h = harness(stage), running = h.run(); await h.entered;
    h.interrupt('new-modal'); h.gate.reject(new Error('old request failed')); await running;
    assert.equal(h.errorBox.hidden, true);
    assert.equal(h.errorBox.textContent, '');
    assert.equal(h.modal.open, true);
    assert.ok(!h.calls.some(value => value.startsWith('toast:')));
    if (stage !== 'accept') assert.ok(h.calls.includes('workspace:s'), 'followup failure retains accepted invalidation');
  });
}

test('the current plan form still finishes, closes and navigates after all followups', async () => {
  const h = harness('workbench-load'), running = h.run(); await h.entered;
  assert.ok(h.calls.indexOf('workspace:s') < h.calls.indexOf('run'));
  assert.equal(h.modal.open, true, 'dialog ownership must survive the final asynchronous read');
  assert.ok(!h.calls.includes('timer'));
  h.gate.resolve(); await running;
  assert.equal(h.modal.open, false);
  assert.equal(h.context.state.timerDuration, 30);
  assert.ok(h.calls.includes('navigate:s/tasks'));
  assert.equal(h.calls.filter(value => value.startsWith('toast:')).length, 1);
});

test('a current followup failure is visible and preserves accepted cache invalidation', async () => {
  const h = harness('detail'), running = h.run(); await h.entered;
  assert.ok(h.calls.includes('workspace:s'));
  h.gate.reject(new Error('detail unavailable')); await running;
  assert.equal(h.saved.s, 'p');
  assert.equal(h.errorBox.hidden, false);
  assert.equal(h.errorBox.textContent, 'detail unavailable');
  assert.equal(h.modal.open, true);
  assert.equal(h.context.state.timerDuration, 99);
});
