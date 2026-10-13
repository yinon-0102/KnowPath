import { esc, storageRead, storageWrite } from './store.js';
import { renderNoteMarkdown } from './notes.js';
import { elapsedText, taskDisplay } from './progress.js';
import { spacePath } from './workbench.js';
import { createActivityController } from './activity.js';

const rows = value => Array.isArray(value) ? value : [];
const active = value => ['queued', 'pending', 'generating', 'running'].includes(value);
const done = value => ['completed', 'succeeded'].includes(value);
const uid = () => globalThis.crypto.randomUUID();
const lessonId = value => value?.lesson_id || value?.id;
export const lessonStyles = [['plain', '通俗易懂'], ['examples_first', '例子优先'], ['concise', '精简重点'], ['deep', '深入讲解']];
export const studyPath = (spaceId, taskId, planId) => spacePath(spaceId, 'study', { task: taskId, plan: planId });

export function createStudyController({ api, storage, changed = () => {}, committed = async () => {}, delay = 2500 }) {
  let state = { key: '', mode: 'original', refs: [], lessons: [], messages: [], style: 'plain', instructions: '' };
  let epoch = 0, sourceEpoch = 0, lessonEpoch = 0, timer, request, chatRequest, lectureRequest, lectureRun;
  const activity = createActivityController({ storage, send: body => api.sessionActivity?.(state.session?.session_id || state.session?.id, body), changed: value => {
    const checkChanged = Boolean(state.needsActivityCheck) !== Boolean(value.needsActivityCheck);
    state.needsActivityCheck = value.needsActivityCheck;
    if (state.session?.status === 'active') { state.session = { ...state.session, paused: value.paused, activity: value.activity || state.session.activity }; emit(checkChanged ? undefined : { sessionOnly: true }); }
  } });
  const attachActivity = () => state.session?.status === 'active' && state.session.task_id === state.task?.id && api.sessionActivity
    ? activity.attach('session:' + (state.session.session_id || state.session.id)).catch(error => { state.activityError = error.message; }) : activity.dispose();
  const chats = new Map();
  const chatFields = ['messages', 'chatSession', 'pendingChat', 'chatStatus', 'chatDraft', 'chatError', 'sending', 'historyCursor', 'questionDraft', 'chatSubmitting'];
  const chatStore = () => state.space && state.nodeId ? `knowpath-study-chats:${state.space.id}:${state.nodeId}` : '';
  function chatContext() {
    if (!state.space || !state.nodeId) return null;
    if (state.mode === 'explanation') {
      const id = lessonId(state.lesson) || state.selectedLessonId || lessonId(state.generation);
      return id ? { kind: 'lesson', lesson_id: id } : null;
    }
    const ref = state.readingRef || state.reader?.ref || state.refs[0];
    return ref ? { kind: 'original', reading_context: { material_id: ref.material_id, material_version_id: ref.material_version_id } } : null;
  }
  const contextKey = context => JSON.stringify([state.space?.id, state.nodeId, state.space?.scope_version, state.space?.bindings, context]);
  function persistChat(entry = state.chat) {
    if (!entry?.storageKey) return;
    const saved = storageRead(storage, entry.storageKey, {});
    const job = entry.pendingChat;
    storageWrite(storage, entry.storageKey, { ...saved, [entry.contextKey]: { chatSession: entry.chatSession, questionDraft: entry.questionDraft || '',
      pendingChat: job ? { run_id: job.run_id, session_id: job.session_id, status: job.status, question: job.question } : null } });
  }
  function detachChat() {
    chatRequest?.abort(); chatRequest = null;
    if (state.chat) { state.chat.subscription = null; state.chat.sending = false; state.chat.chatDraft = null; persistChat(); }
  }
  function invalidateChat(entry = state.chat) {
    if (!entry) return;
    if (entry === state.chat) { chatRequest?.abort(); chatRequest = null; }
    entry.accessEpoch = (entry.accessEpoch || 0) + 1;
    entry.invalid = true; entry.sending = false; entry.chatSubmitting = false; entry.chatDraft = null; entry.messages = [];
    entry.pendingChat = null; entry.questionDraft = ''; entry.historyLoaded = false; entry.chatSession = null;
    persistChat(entry);
  }
  function activateChat() {
    const context = chatContext(), key = context ? contextKey(context) : '', storageKey = chatStore();
    if (state.chatContextKey === key && state.chat?.storageKey === storageKey) return;
    detachChat();
    state.chatContext = context; state.chatContextKey = key;
    const identity = JSON.stringify([storageKey, key]);
    if (!chats.has(identity)) {
      const saved = key ? storageRead(storage, storageKey, {})[key] || {} : {};
      chats.set(identity, { messages: [], questionDraft: '', ...saved, storageKey, contextKey: key });
    }
    state.chat = chats.get(identity);
    state.chat.context = context;
    if (!state.chat.invalid && state.chat.chatSession && !state.chat.historyLoaded && api.studyMessages) void loadHistory(state.chat).catch(error => {
      if (state.chat === chats.get(identity) && error.name !== 'AbortError') { state.chat.chatError = error.message; emit(); }
    });
  }
  const keys = new Map();
  const keyFor = (name, body, stateKey = state.key) => {
    const signature = JSON.stringify([name, stateKey, body]);
    const saved = storageRead(storage, 'knowpath-study-commands', {});
    if (!keys.has(signature)) keys.set(signature, saved[signature] || uid());
    storageWrite(storage, 'knowpath-study-commands', { ...saved, [signature]: keys.get(signature) });
    return keys.get(signature);
  };
  const clearKey = (name, body, stateKey = state.key) => {
    const signature = JSON.stringify([name, stateKey, body]), saved = storageRead(storage, 'knowpath-study-commands', {});
    delete saved[signature]; keys.delete(signature); storageWrite(storage, 'knowpath-study-commands', saved);
  };
  const memoryKey = () => 'knowpath-study:' + state.key;
  const remember = values => storageWrite(storage, memoryKey(), { ...storageRead(storage, memoryKey(), {}), ...values });
  const emit = (change) => { activateChat(); for (const field of chatFields) state[field] = state.chat?.[field]; changed(change); };
  const releaseReader = () => { if (state.reader?.url) URL.revokeObjectURL(state.reader.url); state.reader = null; };
  const stopLecture = () => { lectureRequest?.abort(); lectureRequest = null; lectureRun = null; state.lessonDraft = null; };
  const stop = () => { clearTimeout(timer); timer = null; request?.abort(); detachChat(); stopLecture(); activity.dispose(); request = null; };
  function refsFor(topics) {
    const found = new Map();
    for (const topic of rows(topics)) for (const ref of rows(topic.source_refs)) {
      if (ref?.material_id && ref.material_version_id && ref.chunk_id) found.set(JSON.stringify([ref.material_id, ref.material_version_id, ref.chunk_id]), ref);
    }
    return [...found.values()];
  }
  function chooseLesson(items = state.lessons) {
    return [...rows(items)].sort((a, b) => Number(b.version || 0) - Number(a.version || 0)).find(item => !item.stale && item.style === state.style && (item.instructions || '') === state.instructions
      && (!state.adaptationFingerprint || item.adaptation_fingerprint === state.adaptationFingerprint)) || null;
  }
  const selectedLesson = () => rows(state.lessons).find(item => lessonId(item) === state.selectedLessonId && done(item.status)) || chooseLesson();
  const orderedMessages = items => [...new Map(rows(items).map(item => [item.id || item.message_id, item])).values()].sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0));
  function streamLesson() {
    const candidate = state.generation;
    if (state.mode !== 'explanation' || !active(candidate?.status) || !candidate.run_id || !api.readLessonRun) { stopLecture(); return; }
    if (lectureRun === candidate.run_id) return;
    stopLecture();
    const ticket = epoch, controller = new AbortController(), runId = candidate.run_id, id = lessonId(candidate);
    lectureRequest = controller; lectureRun = runId;
    const valid = () => ticket === epoch && !controller.signal.aborted && lectureRun === runId;
    void api.readLessonRun({ run_id: runId }, { signal: controller.signal,
      onStatus: text => { if (valid()) { state.lessonStatus = text; emit(); } },
      onEvent: (name, data) => {
        if (!valid() || data.lesson_id && data.lesson_id !== id) return;
        if (name === 'lesson.section') state.lessonDraft = { id, sections: rows(data.sections), citations: rows(data.citations) };
        if (name === 'lesson.reset' || name === 'run.failed' || name === 'run.cancelled') state.lessonDraft = null;
        if (name === 'lesson.stage') state.lessonStatus = ({ reading_sources: '正在读取节点原文…', generating: '正在整理讲解，完成的小节会依次显示…', validating_sources: '正在核对讲解与原文依据…' })[data.stage] || '正在生成讲解…';
        emit();
      }
    }).then(async () => {
      if (!valid()) return;
      state.lessonDraft = null;
      await refreshLessons();
    }).catch(error => {
      if (!valid()) return;
      state.lessonDraft = null; state.lessonError = error.message; emit();
    }).finally(() => { if (lectureRequest === controller) { lectureRequest = null; lectureRun = null; } });
  }
  function schedule() {
    clearTimeout(timer);
    if (rows(state.lessons).some(item => active(item.status))) timer = setTimeout(() => void refreshLessons().catch(error => { state.error = error.message; emit(); }), delay);
  }
  async function refreshLessons({ read = true } = {}) {
    if (!state.task) return;
    const ticket = epoch;
    const result = await api.studyLessons(state.space.id, state.nodeId, { signal: request?.signal });
    if (ticket !== epoch) return;
    state.lessons = rows(result.items); state.adaptationContext = result.adaptation_context; state.adaptationFingerprint = result.current_adaptation_fingerprint || result.adaptation_fingerprint; const candidate = chooseLesson();
    state.generation = candidate;
    const selected = selectedLesson();
    if (read && state.mode === 'explanation' && !state.lessonReading && done(selected?.status) && lessonId(selected) !== lessonId(state.lesson)) await readLesson(lessonId(selected), { explicit: false });
    streamLesson(); schedule(); emit();
  }
  async function load({ space, plan, activeSession = null, taskId, nodeId, current = true, materials = [], capabilityError = '' }, { refresh = false } = {}) {
    const selected = storageRead(storage, 'knowpath-study-selection:' + space.id, '');
    const tasks = rows(plan?.tasks).filter(task => !task.historical && !task.context?.historical);
    const task = tasks.find(t => taskId && t.id === taskId || nodeId && (t.node_id || t.context?.node_id || t.id) === nodeId)
      || tasks.find(t => activeSession && t.id === activeSession.task_id)
      || tasks.find(t => (t.node_id || t.context?.node_id || t.id) === selected)
      || tasks.find(t => ['pending', 'in_progress'].includes(t.status)) || tasks[0];
    const stableId = task?.node_id || task?.context?.node_id || task?.id;
    const key = JSON.stringify([space.id, plan?.id, task?.id, stableId, space.scope_version]);
    if (!refresh && state.key === key) { state.space = space; state.plan = plan; state.task = task; state.activeSession = activeSession; state.capabilityError = capabilityError; if (activeSession?.task_id === task?.id) state.session = activeSession; await attachActivity(); emit(); return; }
    stop(); releaseReader(); sourceEpoch++; const ticket = ++epoch;
    state = { key, space, plan, task, nodeId: stableId, current, activeSession, materials, capabilityError, busy: Boolean(task), error: '', mode: 'original', refs: [], lessons: [], messages: [], style: 'plain', instructions: '' };
    const saved = storageRead(storage, memoryKey(), {}), prefs = space.profile?.preferences?.value || {};
    state.mode = saved.mode === 'explanation' ? 'explanation' : 'original';
    state.selectedLessonId = saved.lessonId;
    state.explicitLesson = saved.explicitLesson === true;
    state.style = prefs.lesson_style || 'plain'; state.instructions = prefs.lesson_instructions || '';
    state.sidebarOpen = saved.sidebarOpen !== false; state.drawerOpen = false;
    if (saved.chatSession && saved.lessonId) {
      const context = { kind: 'lesson', lesson_id: saved.lessonId }, key = contextKey(context), storageKey = chatStore();
      if (!storageRead(storage, storageKey, {})[key]) persistChat({ storageKey, contextKey: key, chatSession: saved.chatSession, pendingChat: saved.pendingChat });
    }
    request = new AbortController(); emit();
    if (!task) return;
    storageWrite(storage, 'knowpath-study-selection:' + space.id, stableId);
    try {
      const results = await Promise.allSettled([api.learningRecord(plan.id, task.id, { signal: request.signal }), api.studyLessons(space.id, stableId, { signal: request.signal })]);
      if (ticket !== epoch) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      const record = results[0].value;
      if (record.space_id !== space.id) throw new Error('学习记录与当前空间不一致。');
      state.record = record; state.topics = rows(record.topics).length ? record.topics : rows(task.context?.topics);
      state.refs = refsFor(state.topics);
      state.sourceIndex = Math.max(0, state.refs.findIndex(ref => JSON.stringify([ref.material_id, ref.material_version_id, ref.chunk_id]) === saved.readerIdentity));
      state.readingRef = state.refs[state.sourceIndex];
      state.session = activeSession?.task_id === task.id ? activeSession : [...rows(record.sessions)].reverse().find(s => s.task_id === task.id && s.plan_id === plan.id);
      if (results[1].status === 'fulfilled') {
        state.lessons = rows(results[1].value.items);
        state.adaptationContext = results[1].value.adaptation_context; state.adaptationFingerprint = results[1].value.current_adaptation_fingerprint || results[1].value.adaptation_fingerprint;
        const prefs = results[1].value.preferences;
        if (prefs) { state.style = prefs.lesson_style || 'plain'; state.instructions = prefs.lesson_instructions || ''; }
      } else state.lessonError = results[1].reason.message;
      state.generation = chooseLesson();
      const selected = selectedLesson();
      if (state.mode === 'explanation' && done(selected?.status)) await readLesson(lessonId(selected), { explicit: false });
      await attachActivity();
      if (state.chat?.chatSession) await loadHistory();
      streamLesson(); schedule();
    } catch (error) { if (ticket === epoch && error.name !== 'AbortError') state.error = error.message; }
    finally { if (ticket === epoch) { state.busy = false; emit(); } }
  }
  async function readSource(index = state.sourceIndex || 0) {
    const ref = state.refs[index]; if (!ref) throw new Error('当前节点没有可定位的原文来源。');
    const ticket = epoch, readTicket = ++sourceEpoch, previous = state.reader;
    state.readingRef = ref; state.sourceIndex = index; state.readerBusy = true; state.readerError = ''; emit();
    try {
      const source = await api.source(ref, state.space.id);
      const response = await api.materialFile(ref.material_id, ref.material_version_id, state.space.id, { signal: request?.signal });
      const blob = await response.blob();
      const isPdf = /application\/pdf/i.test(blob.type), text = isPdf ? '' : await blob.text();
      const filename = response.headers?.get('content-disposition') || source.material_name || ref.material_name || '';
      const kind = isPdf ? 'pdf' : /(?:text\/(?:x-)?markdown)|\.md(?:["';\s]|$)/i.test(blob.type + ' ' + filename) ? 'markdown' : 'text';
      if (ticket !== epoch || readTicket !== sourceEpoch) return;
      state.chat.invalid = false;
      const identity = JSON.stringify([ref.material_id, ref.material_version_id, ref.chunk_id]);
      if (previous?.identity === identity && previous.kind === kind) { state.reader = previous; return; }
      releaseReader();
      const saved = storageRead(storage, memoryKey(), {});
      const page = saved.readerIdentity === identity ? Number(saved.page) || source.page || ref.page || 1 : source.page || ref.page || 1;
      const nameMatch = /filename\*=UTF-8''([^;]+)|filename="([^"]+)"|filename=([^;]+)/i.exec(filename);
      let name = source.material_name || ref.material_name || rows(state.materials).find(m => m.id === ref.material_id)?.name || '';
      if (nameMatch) { try { name = nameMatch[1] ? decodeURIComponent(nameMatch[1]) : nameMatch[2] || nameMatch[3]; } catch { /* Keep the source name if the header is malformed. */ } }
      state.reader = { identity, ref, source, name, page, kind, format: 'reading', url: isPdf ? URL.createObjectURL(blob) : null, text, index, token: uid() };
      remember({ readerIdentity: identity, page });
    } catch (error) {
      if (ticket === epoch && readTicket === sourceEpoch) { releaseReader(); state.readerError = error.message; invalidateChat(); }
      throw error;
    } finally { if (ticket === epoch && readTicket === sourceEpoch) { state.readerBusy = false; emit(); } }
  }
  async function readParsedSource(index = 0) {
    const ref = state.refs[index]; if (!ref) throw new Error('没有可定位的解析片段。');
    const ticket = epoch, readTicket = ++sourceEpoch;
    releaseReader(); state.readingRef = ref; state.sourceIndex = index; state.readerError = ''; state.readerBusy = true; emit();
    try {
      const source = await api.source(ref, state.space.id);
      if (ticket !== epoch || readTicket !== sourceEpoch) return;
      state.chat.invalid = false;
      state.reader = { identity: JSON.stringify([ref.material_id, ref.material_version_id, ref.chunk_id]), ref, source, page: source.page || ref.page, kind: 'parsed', text: source.text || '', index, token: uid() };
    } catch (error) { if (ticket === epoch && readTicket === sourceEpoch) { state.readerError = error.message; invalidateChat(); } throw error; }
    finally { if (ticket === epoch && readTicket === sourceEpoch) { state.readerBusy = false; emit(); } }
  }
  function page(value) { const number = Number(value); if (!Number.isInteger(number) || number < 1 || !state.reader) return; state.reader.page = number; state.reader.token = uid(); remember({ page: number }); emit(); }
  function readerFormat(value) { if (state.reader?.kind !== 'markdown') return; state.reader.format = value === 'source' ? 'source' : 'reading'; state.reader.token = uid(); emit(); }
  async function readLesson(id, { explicit = true } = {}) {
    const ticket = epoch, readTicket = ++lessonEpoch; state.selectedLessonId = id; state.lessonReading = true;
    if (explicit) { state.explicitLesson = true; remember({ explicitLesson: true }); } emit();
    try {
      const lesson = await api.studyLesson(id, { signal: request?.signal });
      if (ticket !== epoch || readTicket !== lessonEpoch) return;
      if (lesson.space_id && lesson.space_id !== state.space.id) throw new Error('讲义与当前空间不一致。');
      state.lesson = lesson; remember({ lessonId: id });
    } finally { if (ticket === epoch && readTicket === lessonEpoch) { state.lessonReading = false; emit(); } }
  }
  async function setMode(mode, { sourceIndex } = {}) {
    state.mode = mode === 'explanation' ? 'explanation' : 'original'; remember({ mode: state.mode });
    if (state.mode === 'original') stopLecture();
    if (state.mode === 'original' && state.refs.length) await readSource(sourceIndex ?? state.reader?.index ?? 0);
    else {
      try { await refreshLessons({ read: false }); } catch (error) { state.lessonError = error.message; }
      let candidate = selectedLesson();
      if (!candidate && !state.lesson) candidate = [...state.lessons].reverse().find(item => done(item.status) && !item.stale);
      if (!state.lesson) state.lesson = null;
      if (done(candidate?.status)) await readLesson(lessonId(candidate), { explicit: false });
      if (!state.explicitLesson && !chooseLesson() && !state.generating && api.generateStudyLesson && state.current && state.plan?.status === 'ready' && state.space.status !== 'archived') {
        try { await generate(); } catch (error) { state.lessonError = error.message; }
      }
      streamLesson(); emit();
    }
  }
  async function preferences(style, instructions) {
    if (!lessonStyles.some(([id]) => id === style)) throw new Error('请选择有效的讲解风格。');
    if (instructions.length > 1000) throw new Error('补充要求最多 1000 字。');
    const ticket = epoch, targetSpace = state.space;
    const result = await api.updateProfile(targetSpace.id, { expected_version: targetSpace.profile_version, preferences: { lesson_style: style, lesson_instructions: instructions.trim() } });
    if (ticket !== epoch) return { ...targetSpace, profile: result.profile, profile_version: result.profile_version };
    state.space = { ...state.space, profile: result.profile, profile_version: result.profile_version };
    stopLecture(); state.style = style; state.instructions = instructions.trim(); state.lesson = null; state.selectedLessonId = null; state.explicitLesson = false; remember({ lessonId: null, explicitLesson: false }); state.generation = chooseLesson();
    if (done(state.generation?.status)) await readLesson(lessonId(state.generation)); streamLesson(); emit(); return state.space;
  }
  function assertExecutable() {
    if (!state.current || state.space.status === 'archived' || state.plan?.status !== 'ready' || state.task?.historical || state.task?.context?.historical || ['skipped', 'deferred'].includes(state.task?.status)) throw new Error('请使用当前有效计划中的节点，或先恢复空间、更新计划。');
  }
  async function generate(regenerate = false) {
    assertExecutable();
    const body = { plan_id: state.plan.id, task_id: state.task.id, style: state.style, instructions: state.instructions, regenerate,
      ...(state.session?.status === 'active' ? { learning_session_id: state.session.session_id } : {}) };
    const ticket = epoch; state.generating = true; state.lessonError = ''; emit();
    try {
      const result = await api.generateStudyLesson(state.space.id, state.nodeId, body, { key: keyFor('generate', body) });
      if (ticket !== epoch) return;
      clearKey('generate', body);
      state.selectedLessonId = null; state.explicitLesson = false; remember({ lessonId: null, explicitLesson: false });
      state.mode = 'explanation'; remember({ mode: 'explanation' });
      await refreshLessons(); return result;
    } catch (error) { if (ticket === epoch) state.lessonError = error.message; throw error; }
    finally { if (ticket === epoch) { state.generating = false; emit(); } }
  }
  async function retry(id) { const result = await api.retryStudyLesson(id, { key: keyFor('retry', id) }); clearKey('retry', id); await refreshLessons(); return result; }
  async function cancel() { if (state.generation?.run_id) { const ticket = epoch, run = state.generation.run_id; stopLecture(); emit(); await api.cancelRun(run); if (ticket === epoch) await refreshLessons(); } }
  async function start() {
    assertExecutable();
    if (state.activeSession?.status === 'active' && state.activeSession.task_id !== state.task.id) throw new Error('当前空间已有活动会话，请先保存或返回该会话。');
    if (state.session?.status === 'active') return;
    if (state.task.status === 'completed') throw new Error('已完成节点请使用复测或查看学习记录。');
    const body = { task_id: state.task.id }, ticket = epoch;
    const result = await api.startSession(state.plan.id, body, { key: keyFor('start', body) });
    if (ticket !== epoch) { await committed(); return; }
    clearKey('start', body);
    state.session = { ...result, space_id: state.space.id, paused: false }; state.activeSession = state.session;
    await attachActivity();
    emit(); await committed();
  }
  async function sessionEvent(type) {
    if (state.session?.status !== 'active') throw new Error('请先开始当前节点的学习会话。');
    const ticket = epoch, sessionId = state.session.session_id;
    if (['pause', 'resume'].includes(type) && api.sessionActivity) { await activity.event(type); return; }
    await api.sessionEvent(sessionId, { type, ...(state.task.topic_ids?.[0] ? { topic_id: state.task.topic_ids[0] } : {}) }, { key: uid() });
    if (ticket === epoch) { if (['pause', 'resume'].includes(type)) state.session.paused = type === 'pause'; emit(['pause', 'resume'].includes(type) ? { sessionOnly: true } : undefined); }
  }
  async function finish() {
    if (state.session?.status !== 'active') throw new Error('当前节点没有正在进行的学习会话。');
    const ticket = epoch, result = await api.finishSession(state.session.session_id);
    if (result.status !== 'finished') throw new Error('后端尚未确认会话保存。');
    if (ticket === epoch) { activity.dispose({ notify: false }); state.session = { ...state.session, ...result }; state.activeSession = null; emit(); }
    await committed(); return result;
  }
  function checkHistory(items, entry) {
    for (const item of rows(items)) {
      if (item.reading_context && (item.reading_context.material_id !== entry.context?.reading_context?.material_id || item.reading_context.material_version_id !== entry.context?.reading_context?.material_version_id)
        || item.lesson_id && item.lesson_id !== entry.context?.lesson_id) throw new Error('问答记录与当前阅读依据不一致。');
    }
    return rows(items);
  }
  async function loadHistory(entry = state.chat) {
    if (entry?.invalid || !entry?.chatSession || !api.studyMessages) return;
    if (entry.historyPromise) return entry.historyPromise;
    const ticket = epoch, spaceId = state.space.id, accessEpoch = entry.accessEpoch;
    entry.historyPromise = (async () => {
      const history = await api.studyMessages(spaceId, entry.chatSession, {}, { signal: request?.signal });
      const items = checkHistory(history.items, entry);
      if (ticket !== epoch || state.chat !== entry || entry.invalid || entry.accessEpoch !== accessEpoch) return;
      entry.messages = orderedMessages(items); entry.historyCursor = history.next_cursor; entry.historyLoaded = true; emit();
    })();
    try { return await entry.historyPromise; } finally { entry.historyPromise = null; }
  }
  async function moreHistory() {
    const ticket = epoch, entry = state.chat, accessEpoch = entry.accessEpoch;
    if (entry.invalid) return;
    const history = await api.studyMessages(state.space.id, entry.chatSession, { cursor: entry.historyCursor });
    const items = checkHistory(history.items, entry);
    if (ticket !== epoch || state.chat !== entry || entry.invalid || entry.accessEpoch !== accessEpoch) return; entry.messages = orderedMessages([...items, ...entry.messages]); entry.historyCursor = history.next_cursor; emit();
  }
  function question(value) { if (!state.chat) activateChat(); state.chat.questionDraft = String(value).slice(0, 8000); state.questionDraft = state.chat.questionDraft; persistChat(); }
  function sidebar(open, { drawer = false } = {}) { if (drawer) state.drawerOpen = Boolean(open); else { state.sidebarOpen = Boolean(open); remember({ sidebarOpen: state.sidebarOpen }); } emit(); }
  async function ask(message, resume = false) {
    if (!resume && state.capabilityError) throw new Error(state.capabilityError);
    activateChat(); const entry = state.chat, context = state.chatContext;
    if (entry.sending || entry.chatSubmitting) return;
    if (!resume && entry.pendingChat) throw new Error('请先继续接收或停止上次的提问。');
    if (!resume && state.mode === 'explanation' && (!state.lesson || state.lesson.stale || !done(state.lesson.status))) throw new Error('请先生成并打开当前节点的有效讲义，再围绕讲义追问。');
    if (!resume && state.mode === 'original') {
      if (!context?.reading_context || state.readerError || state.readerBusy) throw new Error('请先打开当前节点的有效原文。');
      const { material_id, material_version_id } = context.reading_context;
      if (!rows(state.space.bindings).some(b => b.material_id === material_id && b.material_version_id === material_version_id)) throw new Error('原文版本与空间绑定不一致，请更新阅读依据。');
    }
    if (!resume && (!state.current || state.space.status === 'archived' || state.plan?.status !== 'ready')) throw new Error('当前节点为只读状态，请使用有效学习计划。');
    if (resume && !entry.pendingChat) throw new Error('当前阅读依据没有待接收任务。');
    const ticket = epoch, controller = new AbortController(), targetSpace = state.space, commandStateKey = state.key, accessEpoch = entry.accessEpoch;
    chatRequest = controller; entry.subscription = controller; entry.sending = true; entry.chatError = ''; entry.cancelRequested = false; emit();
    const valid = () => ticket === epoch && state.chat === entry && !controller.signal.aborted;
    try {
      if (!resume) {
        const body = { message, stream: true, answer_mode: 'auto', ...(context.kind === 'original' ? { reading_context: context.reading_context } : { lesson_id: context.lesson_id }), ...(entry.chatSession ? { session_id: entry.chatSession } : {}) };
        const commandKey = keyFor('ask', body, commandStateKey); entry.chatSubmitting = true;
        entry.submission = controller;
        let job;
        try { job = await api.sendStudyMessage(targetSpace.id, body, { key: commandKey }); }
        finally { if (entry.submission === controller) { entry.submission = null; entry.chatSubmitting = false; } }
        clearKey('ask', body, commandStateKey);
        if (entry.invalid || entry.accessEpoch !== accessEpoch) { await api.cancelRun(job.run_id); return; }
        entry.chatSession = job.session_id; entry.pendingChat = { ...job, question: message }; entry.questionDraft = ''; persistChat(entry);
        if (entry.cancelRequested) { await api.cancelRun(job.run_id); entry.pendingChat = null; persistChat(entry); throw new DOMException('操作已停止', 'AbortError'); }
        if (!valid()) { if (state.chat === entry) emit(); return; }
      }
      // Replaying from zero reaudits previously received prose before revealing it.
      const job = entry.pendingChat; job.lastEventId = '0'; job.streamText = ''; job.textPhase = null; delete job.result;
      const result = await api.readMessage(job, { signal: controller.signal, onStatus: status => { if (valid()) { entry.chatStatus = status; emit(); } },
        onDelta: (_, value) => { if (valid()) { entry.chatDraft = { question: job.question, text: value.text, provisional: value.provisional, answer_mode: value.answer_mode, supplement_explanation: value.supplement_explanation }; emit(); } }
      });
      if (!valid()) return;
      entry.chatDraft = null; entry.pendingChat = null; persistChat(entry); await loadHistory(entry);
      return result;
    } catch (error) {
      if (entry.subscription === controller) {
        entry.chatDraft = null; if (error.code !== 'RUN_PENDING' && error.name !== 'AbortError' && error.status >= 400) entry.pendingChat = null;
        const invalidContext = ['STALE_LEARNING_CONTEXT', 'MATERIAL_OUT_OF_SCOPE', 'SOURCE_NOT_FOUND', 'MATERIAL_NOT_FOUND'].includes(error.code) || [404, 410].includes(error.status);
        if (invalidContext) { invalidateChat(entry); entry.chatError = `${error.message} 请更新阅读依据。`; }
        else if (valid() && error.name !== 'AbortError') entry.chatError = error.message;
        persistChat(entry);
      }
      throw error;
    } finally {
      if (entry.subscription === controller) { entry.subscription = null; entry.sending = false; entry.chatSubmitting = false; }
      if (chatRequest === controller) chatRequest = null; if (state.chat === entry) emit();
    }
  }
  async function cancelChat() { const entry = state.chat, job = entry.pendingChat; entry.cancelRequested = true; chatRequest?.abort(); entry.chatDraft = null; emit(); if (job) await api.cancelRun(job.run_id); entry.pendingChat = null; persistChat(entry); if (state.chat === entry) emit(); }
  function dispose() { stop(); epoch++; sourceEpoch++; releaseReader(); state = { key: '', refs: [], lessons: [], messages: [], mode: 'original', style: 'plain', instructions: '' }; }
  return { snapshot: () => state, load, refreshLessons, setMode, readSource, readParsedSource, page, readerFormat, readLesson, preferences, generate, retry, cancel, start, sessionEvent, finish, ask, cancelChat, loadHistory, moreHistory, question, sidebar, dispose };
}

const action = (label, name, attrs = '', primary = false) => `<button type="button" class="button ${primary ? 'button-primary' : 'button-secondary'}" data-study-action="${name}" ${attrs}>${label}</button>`;
const info = (text, error = false) => `<p class="notice ${error ? 'error' : ''}" role="${error ? 'alert' : 'status'}">${esc(text)}</p>`;
const title = task => taskDisplay(task).title;
export function renderAnswerMetadata(answer = {}) {
  const label = ({ material: '资料回答', general: '模型回答', mixed: '资料回答 + 模型补充' })[answer.answer_mode];
  if (!label) return '';
  return `<div class="answer-metadata"><span class="badge">${label}</span>${answer.supplement_explanation ? `<p class="subtle">${esc(answer.supplement_explanation)}</p>` : ''}</div>`;
}
export function renderStudyReader(view) {
  const reader = view.reader;
  if (view.readerBusy && reader) return info('正在核对原文来源…') + `<div class="study-document" data-study-document data-reader-token="${esc(reader.token)}" hidden></div>`;
  if (view.readerError) return info(view.readerError, true) + action('重试原文阅读', 'source', `data-index="${Number(view.sourceIndex) || 0}"`) + action('改读解析文本片段', 'parsed-source', `data-index="${Number(view.sourceIndex) || 0}"`);
  if (!reader) return info(view.readerBusy ? '正在读取原始资料…' : '选择来源开始阅读。') + (view.refs?.length ? action('阅读原文', 'source', 'data-index="0"', true) : '');
  const locator = reader.kind === 'pdf' && reader.page ? `第 ${Number(reader.page)} 页` : reader.kind === 'markdown' ? 'Markdown 原文' : '原文内容';
  const formatLabel = reader.kind === 'parsed' ? '解析文本片段（原文件另行阅读）' : '原始文件内容';
  const markdown = reader.kind === 'markdown';
  const switches = markdown ? `<div class="study-reader-formats" role="group" aria-label="Markdown 显示方式">${['reading', 'source'].map(value => `<button type="button" data-study-action="reader-format" data-format="${value}" aria-pressed="${(reader.format || 'reading') === value}">${value === 'reading' ? '排版阅读' : '查看源码'}</button>`).join('')}</div>` : '';
  const body = markdown && reader.format !== 'source' ? `<article class="study-original-markdown note-markdown">${renderNoteMarkdown(reader.text)}</article>` : `<article class="study-original-text">${esc(reader.text)}</article>`;
  return `<div class="study-reader-toolbar"><span>${esc(locator)}</span>${reader.kind === 'pdf' ? `<label>页码 <input type="number" min="1" value="${Number(reader.page) || 1}" data-study-page aria-label="定位原 PDF 页码"></label><a href="${esc(reader.url)}#page=${Number(reader.page) || 1}&view=FitH" target="_blank" rel="noopener" class="text-link" data-study-open-original>在新标签页打开原 PDF</a>` : `<span>${formatLabel}</span>${switches}`}</div><div class="study-document" data-study-document data-reader-token="${esc(reader.token)}">${reader.kind === 'pdf' ? `<iframe class="study-pdf" src="${esc(reader.url)}#page=${Number(reader.page) || 1}&view=FitH" title="原始 PDF 文档，${esc(locator)}"></iframe>` : body}</div>${reader.kind === 'pdf' ? '<p class="subtle">相关页码由原文来源定位。浏览器不支持内嵌 PDF 时，可在新标签页阅读。</p>' : ''}`;
}
export function renderStudySidebar(view, readOnly = false) {
  const original = view.mode !== 'explanation', context = view.chatContext;
  const ref = view.readingRef || view.reader?.ref || rows(view.refs)[0];
  const name = view.reader?.ref?.material_id === ref?.material_id && view.reader?.name || ref?.material_name || rows(view.materials).find(m => m.id === ref?.material_id)?.name || '当前原文';
  const bound = rows(view.space?.bindings).some(b => b.material_id === ref?.material_id && b.material_version_id === ref?.material_version_id);
  const ready = original ? Boolean(context?.reading_context && bound && !view.readerBusy && !view.readerError) : Boolean(view.lesson && !view.lesson.stale && done(view.lesson.status));
  const disabled = readOnly || Boolean(view.capabilityError) || !ready || view.sending || view.chatSubmitting;
  const messages = rows(view.messages).map(message => `<div class="study-message"><span class="subtle">你</span><p>${esc(message.message || message.user_text || '')}</p>${message.response ? `<span class="subtle">学习助手</span>${renderAnswerMetadata(message.response)}<div class="note-markdown">${renderNoteMarkdown(message.response.text || '')}</div><div class="study-citations">${rows(message.response.citations).map((ref, index) => `<button type="button" class="text-link" data-study-action="message-citation" data-message="${esc(message.id || message.message_id)}" data-index="${index}">查看依据 ${index + 1}</button>`).join('')}</div>` : `<p class="subtle">${active(message.status) ? '正在生成回答' : '未生成有效回答'}</p>`}</div>`).join('');
  const draft = view.chatDraft?.text ? `<div class="study-message study-stream-draft">${renderAnswerMetadata(view.chatDraft)}<span class="subtle">你</span><p>${esc(view.chatDraft.question || '')}</p><span class="subtle">学习助手 · ${view.chatDraft.provisional ? '正在生成，依据待核对' : '正在接收'}</span><div class="note-markdown">${renderNoteMarkdown(view.chatDraft.text)}</div></div>` : '';
  return `<button type="button" class="study-sidebar-backdrop" data-study-action="sidebar-close" aria-label="关闭学习问答" ${view.drawerOpen ? '' : 'hidden'}></button><aside id="study-sidebar" class="panel study-sidebar" data-study-sidebar data-chat-context="${esc(view.chatContextKey || '')}" aria-labelledby="study-sidebar-title"><div class="study-sidebar-heading"><h2 id="study-sidebar-title">${original ? '询问原文' : '继续问讲解'}</h2><button type="button" class="study-sidebar-close" data-study-action="sidebar-close" aria-label="收起学习问答">×</button></div><div class="study-sidebar-context"><strong>${original ? esc(name) : `讲义版本 ${esc(view.lesson?.version || view.generation?.version || '—')}`}</strong><p>${original ? '资料相关问题按当前文档回答 · 其他问题由模型回答' : '结合讲义和原文回答 · 资料不足时标注模型补充'}</p>${original && ref ? action('查看原文', 'source', `data-index="${Number(view.sourceIndex) || 0}"`) : view.lesson ? action('查看讲义依据', 'lesson-sources') : ''}</div><div class="study-sidebar-messages" data-study-chat-scroll role="log" aria-label="当前阅读依据的问答记录" aria-live="polite" aria-relevant="additions">${view.historyCursor ? action('更早的问答', 'history') : ''}${messages}${draft}${!messages && !draft ? `<p class="study-sidebar-empty">${original ? '可以询问原文，也可以自由交流。资料回答附来源，模型补充会明确标注。' : '围绕讲义继续提问，或请助手换一种方式解释。'}</p>` : ''}</div><div class="study-sidebar-compose">${view.capabilityError ? info(view.capabilityError, true) : ''}${!ready ? info(original ? '请先打开空间绑定版本的有效原文。' : '请等待讲义生成完成，或切回原文提问。') + (!original ? action('切回原文提问', 'mode', 'data-mode="original"') : '') : ''}${view.chatError ? info(view.chatError, true) : ''}${view.sending || view.chatSubmitting ? info(view.chatStatus || '正在提交或生成回答…') : view.pendingChat ? info('接收已暂停，可继续接收同一个任务。') : ''}<form id="study-question" data-chat-context="${esc(view.chatContextKey || '')}"><label class="screenreader" for="study-question-input">${original ? '针对当前原文提问' : '针对当前讲义提问'}</label><textarea id="study-question-input" name="message" rows="3" maxlength="8000" required placeholder="${original ? '这份文档中，哪一点还不明白？' : '继续问讲义中的概念、例子或易错点'}" ${disabled ? 'disabled' : ''}>${esc(view.questionDraft || '')}</textarea><div class="study-question-actions">${view.sending || view.chatSubmitting ? action('停止生成', 'cancel-chat') : view.pendingChat ? action('继续接收', 'resume-chat', '', true) + action('停止任务', 'cancel-chat') : `<button type="submit" class="button button-primary" ${disabled ? 'disabled' : ''}>发送问题</button>`}</div></form><a class="text-link study-sidebar-free" href="${spacePath(view.space.id, 'assistant')}">空间自由问答</a></div></aside>`;
}
export function renderStudy(view) {
  const { space, plan, task } = view;
  if (!space) return info('正在读取学习空间…');
  if (!plan || !task) return `<section class="panel study-empty"><h2>从一组学习节点开始</h2><p>选择学习范围，再生成学习计划。</p><button class="button button-primary" data-action="new-plan">生成学习计划</button><a class="text-link" href="${spacePath(space.id, 'materials')}">选择资料与范围</a></section>`;
  const session = view.session, sameActive = session?.status === 'active' && session.task_id === task.id;
  const readOnly = !view.current && view.current !== undefined || space.status === 'archived' || plan.status !== 'ready' || task.historical || task.context?.historical || ['skipped', 'deferred'].includes(task.status);
  const generator = view.generation, generating = active(generator?.status) || view.generating;
  const scopeRefs = rows(view.sources?.length ? view.sources : view.refs);
  const scopeTitles = [...new Set([...scopeRefs.map(ref => rows(ref.section_path).join(' / ')), ...rows(view.topics).map(topic => topic.name || topic.title)].filter(Boolean))];
  const scopePages = [...new Set(scopeRefs.map(ref => Number(ref.page)).filter(page => page > 0))].sort((a,b) => a-b);
  const readingBoundary = scopeTitles.length ? `<p class="notice study-reading-boundary"><strong>本节点学习范围：</strong>${scopeTitles.map(esc).join('；')}${scopePages.length ? ` · 第 ${scopePages[0]}${scopePages.length > 1 ? `—${scopePages.at(-1)}` : ''} 页` : ''}。${scopePages.length ? 'PDF 页面可能包含相邻节点内容，请以章条范围为准。' : ''}</p>` : '';
  const noteStatus = session?.status === 'finished' && !rows(view.record?.assessments).some(a => a.status === 'completed') ? '已保存学习会话 · 待测评总结' : '';
  const chapterPath = spacePath(space.id, 'notes', { chapter: view.record?.note?.chapter_id });
  const activityTime = session?.activity || session?.context?.activity;
  const timingNotice = activityTime ? info(`服务端确认学习时间：${elapsedText(activityTime.active_seconds)} · 暂停时间：${elapsedText(activityTime.paused_seconds)}${activityTime.reliable === false ? ' · 历史计时可靠性不足，不用于个性化预测' : ''}。可核对记录；时间预算不会限制阅读。`) : '';
  const planNotice = timingNotice + (view.needsActivityCheck ? info('已有 15 分钟没有页面操作，请核对是否仍在阅读。操作页面后提示会消失，学习不会自动暂停。') : '') + (plan.status === 'needs_replan' ? info('学习依据或目标已变化，请保存当前会话后重新规划。') + '<button type="button" class="button button-secondary" data-action="new-plan">重新规划</button>' : '');
  const sources = rows(view.refs).map((ref, index) => `<button type="button" class="study-source ${view.reader?.index === index ? 'selected' : ''}" data-study-action="source" data-index="${index}">${esc(ref.material_name || ref.filename || rows(ref.section_path).join(' / ') || `来源 ${index + 1}`)}${ref.page ? ` · 第 ${Number(ref.page)} 页` : ''}</button>`).join('');
  const visibleLesson = view.lesson || view.lessonDraft;
  const sections = rows(visibleLesson?.sections).map(section => `<section class="study-lesson-section"><h3>${esc(section.title || ({ core: '核心知识', explanation: '通俗讲解', examples: '示例', pitfalls: '易错点' })[section.kind] || '讲解')}</h3><div class="note-markdown">${renderNoteMarkdown(section.markdown || '')}</div><div class="study-citations">${rows(section.citation_ids).map(id => {
    const ref = rows(visibleLesson.citations).find(ref => ref.id === id || ref.chunk_id === id); const index = rows(visibleLesson.citations).indexOf(ref);
    return ref ? `<button class="text-link" data-study-action="citation" data-index="${index}">查看原文依据${ref.page ? ` · 第 ${Number(ref.page)} 页` : ''}</button>` : '';
  }).join('')}</div></section>`).join('');
  const explanation = `<div class="study-preference-bar"><form id="study-preferences"><label>讲解风格<select name="style" class="select-control">${lessonStyles.map(([id, text]) => `<option value="${id}" ${view.style === id ? 'selected' : ''}>${text}</option>`).join('')}</select></label><label class="study-instructions">补充要求<input name="instructions" maxlength="1000" value="${esc(view.instructions || '')}" placeholder="例如：我是零基础，请解释术语"></label><button class="button button-secondary" type="submit" ${readOnly ? 'disabled' : ''}>保存偏好</button></form></div>${view.lessonError ? info(view.lessonError, true) : ''}${generating ? info(view.lessonStatus || '正在整理节点讲义，可继续阅读原文。') + action('停止生成', 'cancel') : ''}${!view.lesson && !generating ? `<div class="study-lesson-empty"><h3>${generator?.status === 'failed' ? '本次讲解生成失败' : generator?.status === 'cancelled' ? '已停止生成' : '按你的方式理解这个节点'}</h3><p>结合本节点的原文整理要点、解释、示例和易错点。点击后生成，并保存到后端。</p>${generator && ['failed', 'cancelled'].includes(generator.status) ? action('重试生成', 'retry', `data-id="${esc(lessonId(generator))}" ${readOnly ? 'disabled' : ''}`, true) : action('生成讲解', 'generate', readOnly || !rows(view.refs).length ? 'disabled' : '', true)}</div>` : ''}${view.lessonDraft && !view.lesson ? `<div class="study-stream-draft"><p class="subtle">讲解正在生成 · 以下小节已核对引用，整份讲义仍待完成</p>${sections}</div>` : ''}${view.lesson ? `${view.lesson.stale ? info('这份讲义的学习依据已变化，请使用当前节点重新生成。') : ''}${rows(view.lesson.uncovered_topic_ids).length ? info('本次讲义仅覆盖部分主题，请结合原文继续阅读。') : ''}<div class="study-lesson-heading"><span class="badge">讲义版本 ${esc(view.lesson.version || '—')}</span><span class="subtle">模型整理 · 可核对原文依据</span>${action('重新生成', 'regenerate', readOnly || generating ? 'disabled' : '')}</div>${sections}` : ''}${rows(view.lessons).length ? `<details class="study-versions"><summary>已保存的讲义版本（${view.lessons.length}）</summary>${[...view.lessons].reverse().map(item => `<div class="list-item"><span>版本 ${esc(item.version || '—')} · ${esc(lessonStyles.find(([id]) => id === item.style)?.[1] || item.style)}${item.stale ? ' · 依据已变化' : ''} · ${esc(({ completed: '已生成', generating: '生成中', failed: '失败', cancelled: '已取消' })[item.status] || item.status)}</span>${done(item.status) ? action('阅读', 'version', `data-id="${esc(lessonId(item))}"`) : ''}</div>`).join('')}</details>` : ''}`;
  const sidebarView = renderStudySidebar(view, readOnly);
  return `<div class="study-workspace${view.drawerOpen ? ' drawer-open' : ''}" data-chat-context="${esc(view.chatContextKey || '')}" data-study-key="${esc(view.key || '')}">${planNotice}${view.busy ? info('正在读取当前节点…') : ''}${view.error ? info(view.error, true) + action('重新加载', 'refresh') : ''}<section class="panel study-task"><div><span class="badge">${readOnly ? '历史或只读节点' : sameActive ? session.paused ? '学习已暂停' : '正在学习' : task.status === 'completed' ? '任务已完成' : '当前学习节点'}</span><h2>${esc(title(task))}</h2><p class="study-task-source subtle">${esc(taskDisplay(task).sourceLabel)}</p><p class="subtle">${rows(view.topics).map(t => esc(t.name || t.title || t.id)).join(' · ')}${task.estimated_minutes != null ? ` · 预计 ${Number(task.estimated_minutes)} 分钟` : ''}</p>${space.goal ? `<p class="study-goal">${esc(space.goal)}</p>` : ''}</div><div class="study-task-actions">${sameActive ? action(session.paused ? '继续学习' : '暂停', session.paused ? 'resume' : 'pause') : !readOnly && task.status !== 'completed' ? action('开始学习并记录', 'start', '', true) : ''}${action('学习记录', 'record')}</div></section><div class="study-reading-layout${view.sidebarOpen === false ? ' sidebar-collapsed' : ''}"><div class="study-reader-column"><div class="study-reading-tools"><button type="button" class="button button-secondary" data-study-action="sidebar-open" aria-controls="study-sidebar" aria-expanded="${view.sidebarOpen !== false}">提问</button></div><section class="panel study-content"><nav class="study-modes" role="tablist" aria-label="选择学习方式"><button role="tab" id="study-original-tab" aria-controls="study-original" aria-selected="${view.mode !== 'explanation'}" data-study-action="mode" data-mode="original">原文阅读</button><button role="tab" id="study-explanation-tab" aria-controls="study-explanation" aria-selected="${view.mode === 'explanation'}" data-study-action="mode" data-mode="explanation">个性化讲解</button></nav><div id="study-original" role="tabpanel" aria-labelledby="study-original-tab" ${view.mode === 'explanation' ? 'hidden' : ''}>${readingBoundary}<div class="study-source-list">${sources || '<p class="subtle">当前节点没有可定位的原文，资料解析完成后再试。</p>'}</div><div data-study-reader>${renderStudyReader(view)}</div></div><div id="study-explanation" role="tabpanel" aria-labelledby="study-explanation-tab" ${view.mode !== 'explanation' ? 'hidden' : ''}><div class="study-explanation-scroll" data-study-lecture-scroll data-lesson-identity="${esc(lessonId(view.lesson) || lessonId(view.lessonDraft) || '')}">${explanation}</div></div></section></div>${sidebarView}</div><section class="panel study-footer"><div><strong>${sameActive ? session.paused ? '当前会话已暂停' : '学习会话进行中' : '当前为阅读浏览'}</strong><p class="subtle">${session?.elapsed_seconds != null ? `已保存起止间隔：${elapsedText(session.elapsed_seconds)} · 包含暂停` : '点击开始学习后记录真实会话时间。'}${noteStatus ? `<br>${noteStatus}` : ''}</p></div><div class="study-footer-actions">${sameActive ? action('结束并测评', 'finish-assess', '', true) + action('仅保存学习', 'finish') : !readOnly ? action(task.status === 'completed' ? '复测此节点' : '测评此节点', 'assess', '', true) : ''}${!readOnly && task.status !== 'completed' ? `<a class="text-link" href="#/manage/task/${encodeURIComponent(plan.id)}/${encodeURIComponent(task.id)}">记录任务完成情况</a>` : ''}<a class="text-link" href="${chapterPath}">学习笔记</a></div></section></div>`;
}
