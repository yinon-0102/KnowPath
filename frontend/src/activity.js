import { storageRead, storageWrite } from './store.js';

// Only the server measures active time. Browser storage holds intent and identity.
export function createActivityController({ send, storage, changed = () => {}, document = globalThis.document,
  window = globalThis.window, setInterval = globalThis.setInterval, clearInterval = globalThis.clearInterval, now = () => Date.now(), disposeEvent = 'disconnect' } = {}) {
  let state = { identity: '', manualPaused: false, hidden: false, waiting: false, disconnected: false, paused: false, activity: null };
  let timer, epoch = 0, queue = Promise.resolve();
  let lastInteraction = now();
  const interaction = () => { lastInteraction = now(); if (state.needsActivityCheck) { state.needsActivityCheck = false; changed(state); } };
  const idleCheck = () => { state.needsActivityCheck = !state.paused && !state.manualPaused && !state.hidden && !state.waiting && !state.disconnected && now() - lastInteraction >= 15 * 60000; };
  const persist = () => { if (state.identity) storageWrite(storage, 'knowpath-activity:' + state.identity, { manualPaused: state.manualPaused }); };
  function event(name) {
    if (!state.identity) return Promise.resolve();
    if (name === 'pause' || name === 'resume') state.manualPaused = name === 'pause';
    if (name === 'hidden' || name === 'visible') state.hidden = name === 'hidden';
    if (name === 'waiting' || name === 'ready') state.waiting = name === 'waiting';
    if (name === 'disconnect' || name === 'reconnect') state.disconnected = name === 'disconnect';
    state.paused = state.manualPaused || state.hidden || state.waiting || state.disconnected;
    idleCheck();
    persist(); changed(state);
    const ticket = epoch, body = { ...state.context, event: name };
    const pending = queue.catch(() => {}).then(() => ticket === epoch ? send?.(body) : null);
    queue = pending.then(result => {
      if (ticket !== epoch || !result) return;
      state.activity = result.activity ?? state.activity;
      state.paused = result.paused ?? (result.activity?.pause_reasons ? result.activity.pause_reasons.length > 0 : state.paused);
      idleCheck();
      state.error = ''; changed(state); return result;
    }).catch(error => { if (ticket === epoch) { state.error = error.message; changed(state); } throw error; });
    return queue;
  }
  const visibility = () => void event(document?.visibilityState === 'hidden' ? 'hidden' : 'visible').catch(() => {});
  const offline = () => void event('disconnect').catch(() => {});
  const online = () => void event('reconnect').catch(() => {});
  function detach() {
    epoch++; clearInterval(timer); timer = null;
    document?.removeEventListener('visibilitychange', visibility);
    for (const name of ['keydown', 'pointerdown', 'scroll']) document?.removeEventListener(name, interaction, true);
    window?.removeEventListener('offline', offline); window?.removeEventListener('online', online);
  }
  function dispose({ notify = true } = {}) {
    // Send immediately while the caller still owns the old session/question.
    const notification = notify && state.identity ? Promise.resolve(send?.({ ...state.context, event: disposeEvent })).catch(() => {}) : Promise.resolve();
    detach(); state.identity = ''; state.needsActivityCheck = false; return notification;
  }
  async function attach(identity, context = {}, initial = 'reconnect') {
    if (state.identity === identity) return;
    dispose(); queue = Promise.resolve(); lastInteraction = now();
    const saved = storageRead(storage, 'knowpath-activity:' + identity, {});
    state = { identity, context, manualPaused: saved.manualPaused === true, hidden: document?.visibilityState === 'hidden', waiting: false, disconnected: false, paused: false, activity: null, needsActivityCheck: false };
    document?.addEventListener('visibilitychange', visibility);
    for (const name of ['keydown', 'pointerdown', 'scroll']) document?.addEventListener(name, interaction, { capture: true, passive: true });
    window?.addEventListener('offline', offline); window?.addEventListener('online', online);
    timer = setInterval(() => event('heartbeat').catch(() => {}), 30000); timer?.unref?.();
    await event(initial);
    if (state.manualPaused) await event('pause');
    if (state.hidden) await event('hidden');
  }
  return { attach, event, snapshot: () => state, dispose };
}
