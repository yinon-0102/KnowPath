import { storageRead, storageWrite, uid, validateFile } from './store.js';

export const workspacePath = (page, ...ids) => '#/manage/' + [page, ...ids].map(encodeURIComponent).join('/');
export function workspaceRoute(value) {
  const decode = segment => { try { return decodeURIComponent(segment); } catch { return ''; } };
  const [page = '', id = '', item = ''] = String(value || '').split('/').map(decode);
  return { page, id, item };
}
export function workspaceFamily(page) {
  if (page.startsWith('material')) return 'materials';
  if (page.startsWith('assessment') || ['evidence', 'learning-state', 'learning-reset'].includes(page)) return 'spaces';
  if (page.startsWith('knowledge')) return 'spaces';
  if (['task', 'session'].includes(page)) return 'spaces';
  return 'spaces';
}
export function workspaceSection(page) {
  if (['task', 'session'].includes(page)) return 'plan';
  if (page.startsWith('assessment')) return 'assessment';
  if (page.startsWith('knowledge') || page === 'space-scope') return 'materials';
  if (['learning-state', 'evidence', 'learning-reset'].includes(page)) return 'review';
  if (['space-settings', 'space-name', 'space-profile', 'export'].includes(page)) return 'settings';
  return 'overview';
}
const arr = value => Array.isArray(value) ? value : [];
const sessionTopics = data => arr(data.session?.context?.topics ?? data.task?.context?.topics ?? data.topics)
  .filter(topic => arr(data.task?.topic_ids).includes(topic.id));
const required = (value, label) => { const text = String(value || '').trim(); if (!text) throw new Error(`请填写${label}。`); return text; };
const confirmed = values => { if (values.get('confirm') !== 'on') throw new Error('请先阅读并勾选确认。'); };
const integer = (value, min, max, label) => { const n = Number(value); if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${label}须为 ${min}—${max} 的整数。`); return n; };
export function dateRange(values) {
  const range = {};
  for (const field of ['from', 'to']) if (values.get(field)) {
    const date = new Date(values.get(field));
    if (!Number.isFinite(date.getTime())) throw new Error('请选择有效日期和时间。');
    range[field] = date.toISOString();
  }
  if (range.from && range.to && range.from >= range.to) throw new Error('结束时间须晚于开始时间。');
  return range;
}
export function updatedBindings(preview, selected) {
  const updates = new Map(arr(preview.available_updates).filter(row => selected.includes(row.material_id)).map(row => [row.material_id, row]));
  if (!updates.size || updates.size !== selected.length) throw new Error('请选择仍然可用的知识更新。');
  const bindings = arr(preview.bindings);
  if ([...updates.keys()].some(id => !bindings.some(b => b.material_id === id))) throw new Error('更新资料不属于当前空间，请刷新。');
  return bindings.map(binding => {
    const row = updates.get(binding.material_id) || binding;
    return { material_id: row.material_id, material_version_id: row.material_version_id, graph_version: row.graph_version };
  });
}
export function taskPayload(values, plan) {
  const status = values.get('status');
  if (!['completed', 'skipped', 'deferred'].includes(status)) throw new Error('请选择任务处理方式。');
  const body = { status, expected_plan_version: plan.version };
  if (values.get('note')?.trim()) body.note = values.get('note').trim();
  if (values.get('reason')?.trim()) body.reason = values.get('reason').trim();
  if (status === 'skipped') body.reason = required(values.get('reason'), '跳过原因');
  if (status === 'deferred') {
    const time = new Date(required(values.get('defer_until'), '延期时间'));
    if (!Number.isFinite(time.getTime()) || time.getTime() <= Date.now()) throw new Error('请选择未来的延期时间。');
    body.defer_until = time.toISOString();
  }
  return body;
}
export function resolutionPayload(values, review, version) {
  confirmed(values);
  const action = values.get('action');
  if (!['correct', 'invalidate', 'reject'].includes(action)) throw new Error('请选择处理方式。');
  const refs = arr(review.frozen_question?.source_refs);
  const indices = values.getAll('source').map(Number);
  if (!indices.length || new Set(indices).size !== indices.length || indices.some(index => !Number.isInteger(index) || !refs[index]) || !review.source_text?.trim()) throw new Error('请选择已核对的封存来源依据。');
  const body = { action, expected_review_version: version, confirmed: true, reason: required(values.get('reason'), '处理理由'), source_refs: indices.map(index => refs[index]) };
  if (action === 'correct') {
    body.corrected_rubric = required(values.get('rubric'), '完整评分标准');
    const answer = values.get('answer_key')?.trim();
    if (review.question?.type === 'single_choice') {
      if (!arr(review.frozen_question.options).some(option => option.id === answer)) throw new Error('请选择题目已有的正确选项。');
      body.corrected_answer_key = answer;
    } else if (answer) body.corrected_answer_key = answer;
  }
  return body;
}
export function correctionPayload(values, target, kind) {
  if (!target) throw new Error('请选择需要纠错的知识点或关系。');
  const action = values.get('action'), source = values.get('source');
  if (!['reject', 'replace'].includes(action)) throw new Error('请选择纠错方式。');
  if (!arr(target.source_refs).some(ref => ref.chunk_id === source)) throw new Error('请选择这个知识项的原文依据。');
  const body = { kind, target_id: target.id, action, source_ref: source, reason: required(values.get('reason'), '纠错原因') };
  if (action === 'replace') {
    body.proposed_value = Object.fromEntries((kind === 'node' ? ['name', 'description'] : ['from_id', 'to_id', 'type']).map(key => [key, String(values.get(key) || '').trim()]).filter(([, value]) => value));
    if (!Object.keys(body.proposed_value).length) throw new Error('请填写更正后的内容。');
  }
  return body;
}

const JOURNAL = 'knowpath-workspace-commands-v1', SESSIONS = 'knowpath-study-sessions-v1', CORRECTIONS = 'knowpath-pending-corrections-v1';
// A command keeps its body, key and response across a timeout or page reload.
// Retrying a queued command polls its original run instead of submitting again.
export function createWorkspaceController({ api, context, changed = () => {}, committed = async () => {}, storage, savePdf = async () => {}, openSource = async () => {}, download = () => {}, navigate = () => {} }) {
  let generation = 0, view = { key: '', route: {}, data: null, busy: '', error: '', notice: '', editRevision: 0, filters: {} }, loading;
  let pending = storageRead(storage, JOURNAL, {});
  if (!pending || typeof pending !== 'object' || Array.isArray(pending)) pending = {};
  const savePending = () => storageWrite(storage, JOURNAL, pending);
  const sessionMap = () => storageRead(storage, SESSIONS, {});
  const correctionMap = () => storageRead(storage, CORRECTIONS, {});
  function sync() {
    // Keep existing pending-operation journal keys so updates can resume an
    // already accepted backend command instead of submitting it again.
    const ctx = context(), key = JSON.stringify(['live', ctx.route]);
    if (key !== view.key) {
      generation++; loading = null;
      view = { key, route: workspaceRoute(ctx.route || ''), data: null, busy: '', error: '', notice: '', editRevision: 0, filters: {} };
    }
    view.pendingRun = Object.entries(pending).find(([slot, entry]) => {
      try { return JSON.parse(slot)[0] === view.key && entry.response?.run_id; } catch { return false; }
    })?.[1]?.response?.run_id || '';
    return view;
  }
  const active = ticket => { sync(); return ticket === generation; };
  async function read(route, filters = {}) {
    const { page, id, item } = route;
    if (page === 'health') return { health: await api.health() };
    if (page.startsWith('material')) {
      const [materials, versions, spaces] = await Promise.all([api.materials(), api.materialVersions(id), api.spaces()]);
      const material = materials.find(row => row.id === id);
      if (!material) throw new Error('资料不存在，请返回资料库。');
      const detail = { material, version: versions.find(row => row.id === material.current_version_id) };
      const result = { ...detail, versions, affected: spaces.filter(space => arr(space.bindings).some(b => b.material_id === id)) };
      if (['material-graph', 'material-reconcile'].includes(page)) result.diff = await api.graphDiff(id);
      return result;
    }
    if (page.startsWith('assessment')) {
      const assessment = await api.assessment(id);
      if (assessment.status !== 'completed') throw new Error('请先完成本轮测评，再查看结果与复核。');
      const [result, reviews, space] = await Promise.all([api.assessmentResult(id), ['assessment-result', 'assessment-grade'].includes(page) ? Promise.resolve(null) : api.questionReviews(id), api.space(assessment.space_id)]);
      if (result.assessment_id !== id || (reviews && reviews.assessment_id !== id)) throw new Error('返回的测评标识不一致，请重试。');
      // Frozen results remain readable even when current knowledge was superseded.
      const topics = await api.topics(space).catch(() => []);
      return { assessment, result, reviews, space, topics };
    }
    if (['task', 'session'].includes(page)) {
      const plan = await api.plan(id), task = arr(plan.tasks).find(task => task.id === item);
      if (!task) throw new Error('这个任务已不存在，请返回学习安排。');
      const space = await api.space(plan.space_id);
      if (space.id !== plan.space_id) throw new Error('任务与学习空间不一致，请刷新。');
      const [topics, workbench] = await Promise.all([api.topics(space), typeof api.workbench === 'function' ? api.workbench(space.id) : Promise.resolve(null)]);
      const local = sessionMap()[space.id] || null;
      let session = local;
      if (typeof api.workbench === 'function') {
        if (workbench?.space_id !== space.id) throw new Error('学习会话与当前空间不一致，请刷新。');
        session = workbench.active_session || (local?.status === 'finished' && local.plan_id === id && local.task_id === item ? local : null);
      }
      if (session) {
        const sessionId = session.session_id || session.id;
        if (!sessionId || session.space_id && session.space_id !== space.id) throw new Error('学习会话与当前空间不一致，请刷新。');
        session = { ...session, session_id: sessionId, route: workspacePath('session', session.plan_id, session.task_id) };
      }
      return { plan, task, space, session, topics };
    }
    const space = await api.space(id);
    if (space.id !== id) throw new Error('空间信息不一致，请刷新。');
    const result = { space };
    if (page === 'space-profile') result.profile = await api.profile(id);
    if (page === 'knowledge-updates') {
      const [updates, materials] = await Promise.all([api.knowledgeUpdates(id), api.materials()]);
      result.updates = { ...updates, available_updates: arr(updates.available_updates).map(update => ({ ...update, material_name: materials.find(row => row.id === update.material_id)?.name })) };
    }
    if (page === 'knowledge-changes') result.history = await api.changes(id, filters);
    if (['learning-state', 'learning-reset', 'evidence', 'space-scope'].includes(page)) {
      result.topics = await api.topics(space);
      if (page === 'evidence') result.evidence = await api.evidence(id, filters);
      else if (page !== 'space-scope') result.state = await api.learningState(id, { ...filters, include_evidence: true });
    }
    if (page === 'knowledge-correct') {
      result.correction = correctionMap()[id] || null;
      if (result.correction) {
        result.diff = await api.graphDiff(result.correction.materialId, result.correction.candidate_revision_id);
        if (result.diff.status === 'published') {
          result.correctionPublished = true;
          const all = correctionMap(); delete all[id]; storageWrite(storage, CORRECTIONS, all);
        }
      } else result.graph = await api.graph(space);
    }
    return result;
  }
  async function load(force = false) {
    sync(); if ((!force && (view.data || loading)) || view.busy) return;
    const ticket = generation, route = view.route, filters = { ...view.filters };
    view.busy = '正在读取…'; view.error = ''; changed();
    loading = read(route, filters);
    try { const data = await loading; if (active(ticket)) { view.data = data; view.editRevision++; } }
    catch (error) { if (active(ticket)) view.error = error.message; }
    finally { if (active(ticket)) { loading = null; view.busy = ''; changed(); } }
  }
  async function executeCommand(origin, operation, body, send, { wait = true, fingerprint = body } = {}) {
    const slot = JSON.stringify([origin, operation]), signature = JSON.stringify(fingerprint);
    let entry = pending[slot];
    if (entry && entry.signature !== signature) throw new Error('上次提交尚未确认。请保持原内容重试，或刷新核对处理结果。');
    if (!entry) { entry = pending[slot] = { key: uid(), signature, body }; savePending(); }
    try {
      if (!entry.response) { entry.response = await send({ key: entry.key }); savePending(); }
      const response = entry.response;
      if (wait && response?.run_id) await api.waitRun(response.run_id);
      else if (wait && ['queued', 'running', 'processing', 'pending'].includes(response?.status)) throw new Error('后端未返回任务标识，暂时无法确认完成。');
      delete pending[slot]; savePending();
      return response;
    } catch (error) {
      // Explicit validation/conflict failures did not execute this command;
      // connection failures and timeouts must retain the original identity.
      if ((error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) || ['failed', 'cancelled'].includes(error.details?.runStatus)) { delete pending[slot]; savePending(); }
      throw error;
    }
  }
  async function run(label, work) {
    sync(); if (view.busy || !view.data) return;
    const ticket = generation, route = { ...view.route }, data = view.data, key = view.key;
    view.busy = label; view.error = ''; view.notice = ''; changed();
    try { await work({ ticket, route, data, key }); }
    catch (error) { if (active(ticket)) view.error = error.message || '操作未完成，请重试。'; }
    finally { if (active(ticket)) { view.busy = ''; changed(); } }
  }
  async function finish(task, message, { reread = true } = {}) {
    // Commit success is independent of whether a later list refresh succeeds.
    if (active(task.ticket)) { view.notice = message; view.editRevision++; }
    try {
      await committed(task.route, task.data);
      if (reread) { const data = await read(task.route, view.filters); if (active(task.ticket)) view.data = data; }
    } catch (error) { if (active(task.ticket)) view.error = `操作已完成，但刷新失败：${error.message} 请刷新查看最新状态。`; }
  }
  async function submit(values) {
    return run('正在提交，请稍候…', async task => {
      const command = (...args) => executeCommand(task.key, ...args);
      const { page, id, item } = task.route, data = task.data;
      const reason = () => required(values.get('reason'), '原因');
      if (page === 'material-name' || page === 'material-archive') {
        if (page === 'material-archive') confirmed(values);
        const body = { expected_version: data.material.version, ...(page === 'material-name' ? { name: required(values.get('name'), '资料名称') } : { status: 'archived' }) };
        await command(page, body, options => api.updateMaterial(id, body, options));
        await finish(task, page === 'material-name' ? '资料名称已保存。' : '资料已归档。');
      } else if (page === 'material-delete') {
        confirmed(values);
        if (data.affected.length && values.get('cascade') !== 'on') throw new Error('请确认删除资料对关联空间的影响。');
        // Material version does not protect bindings added in another tab.
        const spaces = await api.spaces();
        const latest = spaces.filter(space => arr(space.bindings).some(binding => binding.material_id === id));
        const snapshot = list => JSON.stringify(list.map(space => [space.id, space.name]).sort((a, b) => a[0].localeCompare(b[0])));
        if (snapshot(latest) !== snapshot(data.affected)) throw new Error('关联空间已变化，请刷新并重新确认影响。');
        const body = { expected_version: data.material.version, confirm: true, cascade: data.affected.length > 0 };
        await command(page, body, options => api.deleteMaterial(id, body, options));
        await finish(task, '资料已删除。', { reread: false });
        if (active(task.ticket)) navigate('#/materials');
      } else if (page === 'material-upload') {
        const file = values.get('file'); validateFile(file);
        const note = String(values.get('note') || '').trim();
        const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))].map(n => n.toString(16).padStart(2, '0')).join('');
        const response = await command(page, { note, filename: file.name, digest }, options => api.uploadVersion(id, file, note, options), { wait: false });
        if (!response.material_version_id) throw new Error('后端未返回资料版本，请刷新版本列表核对。');
        let pdfWarning = '';
        if (/\.pdf$/i.test(file.name)) {
          try { await savePdf(file, id, response.material_version_id); } catch { pdfWarning = ' PDF 本地副本保存失败，阅读时可重新关联。'; }
        }
        await finish(task, '新版本已上传，解析在后台进行。解析完成后请审核知识结构。' + pdfWarning);
        if (active(task.ticket)) { view.data.uploaded = response; }
      } else if (page === 'material-ingest' || page === 'material-reconcile') {
        const version = data.versions.find(v => v.id === values.get('version_id'));
        if (!version) throw new Error('请选择一个资料版本。');
        const body = { version_id: version.id, ...(page === 'material-reconcile' ? { expected_graph_version: data.diff.base_graph_version } : {}) };
        await command(page, body, options => page === 'material-ingest' ? api.ingest(id, body, options) : api.reconcile(id, body, options));
        await finish(task, '处理完成。请进入知识结构审核检查本次变更。');
      } else if (page === 'material-graph') {
        confirmed(values);
        if (data.diff.status !== 'pending_review') throw new Error('当前没有待发布的图谱修订。');
        if (data.diff.correction_id) throw new Error('这是知识纠错候选，请从学习空间的知识纠错页面确认发布。');
        const resolutions = arr(data.diff.conflicts).map((conflict, i) => {
          const action = values.get('conflict-' + i), reason = required(values.get('reason-' + i), '冲突处理理由');
          if (!['keep_old', 'use_new', 'keep_both'].includes(action)) throw new Error('请处理全部冲突。');
          return { conflict_id: conflict.conflict_id, action, reason };
        });
        const body = { revision: data.diff.candidate_revision_id, version: data.diff.base_graph_version, resolutions };
        await command(page, body, options => api.publish(id, body.revision, body.version, resolutions, options));
        await finish(task, '图谱已发布。已有学习空间可在“知识更新”中确认采用。');
      } else if (page === 'space-name') {
        const body = { name: required(values.get('name'), '空间名称'), expected_version: data.space.space_version };
        await command(page, body, options => api.updateSpace(id, body, options)); await finish(task, '空间名称已保存。');
      } else if (page === 'space-profile') {
        const body = { goal: required(values.get('goal'), '学习目标'), preferences: { example_first: values.get('example_first') === 'on', concise_explanations: values.get('concise_explanations') === 'on' }, expected_version: data.profile.profile_version };
        const budget = String(values.get('weekly_minutes') || '').trim();
        if (budget) body.weekly_minutes = integer(budget, 15, 2400, '每周学习时间');
        if (values.has('target_date')) {
          const date = String(values.get('target_date') || '').trim();
          if (date && (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(new Date(date).getTime()) || new Date(date).toISOString().slice(0, 10) !== date)) throw new Error('请选择有效的目标日期。');
          body.target_date = date || null;
        }
        await command(page, body, options => api.updateProfile(id, body, options)); await finish(task, '学习目标与偏好已保存。需要时可在空间内重新安排任务。');
      } else if (page === 'space-scope') {
        const included = values.getAll('topic_ids'), excluded = values.getAll('excluded_topic_ids');
        if (!included.length) throw new Error('请选择至少一个学习主题。');
        if (included.some(id => excluded.includes(id))) throw new Error('同一个主题不能同时纳入和排除。');
        const body = { included, excluded, prerequisites: values.get('prerequisites') === 'on', version: data.space.space_version };
        await command(page, body, options => api.setScope(data.space, included, excluded, body.prerequisites, options)); await finish(task, '学习范围已保存，请检查相关计划是否需要重新安排。');
      } else if (page === 'knowledge-updates') {
        confirmed(values);
        const body = { bindings: updatedBindings(data.updates, values.getAll('materials')), expected_space_version: data.space.space_version };
        await command(page, body, options => api.applyKnowledgeUpdates(id, body, options)); await finish(task, '已采用选择的知识版本，相关学习状态和计划以最新结果为准。');
      } else if (page === 'learning-reset') {
        confirmed(values); const ids = values.getAll('topic_ids');
        if (!ids.length) throw new Error('请选择要重置的主题。');
        const body = { topic_ids: ids, expected_state_version: data.state.state_version, reason: reason() };
        await command(page, body, options => api.resetState(id, body, options)); await finish(task, '已重置所选主题，历史证据仍可追溯。');
      } else if (page === 'assessment-grade' || page === 'assessment-report') {
        const question = arr(data.assessment.questions).find(q => q.id === item);
        if (!question) throw new Error('题目已不存在，请重新打开测评结果。');
        const status = arr(data.result.question_results).find(row => row.question_id === item)?.question_review_status;
        if (status === 'pending' || page === 'assessment-grade' && status === 'invalid') throw new Error('这道题当前不可复核评分或重复反馈，请查看处理记录。');
        const body = { question_id: item, reason: reason(), ...(page === 'assessment-report' ? { expected_review_version: data.reviews.review_version } : {}) };
        await command(page, body, options => page === 'assessment-grade' ? api.gradeReview(id, body, options) : api.reportQuestion(id, body, options));
        await finish(task, page === 'assessment-grade' ? '评分复核已完成，可返回逐题结果查看最新反馈。' : '题目反馈已记录，该题已进入待复核状态。');
      } else if (page === 'assessment-resolve') {
        const review = arr(data.reviews.items).find(row => row.review_id === item);
        if (!review || review.status !== 'pending' || arr(review.events).some(event => event.action !== 'report')) throw new Error('该反馈已处理，请刷新查看记录。');
        const body = resolutionPayload(values, review, data.reviews.review_version);
        await command(page, body, options => api.resolveQuestion(id, item, body, options)); await finish(task, '题目处理已完成，结果和学习证据已更新。');
      } else if (page === 'task') {
        const body = taskPayload(values, data.plan);
        await command(page, body, options => api.updateTask(id, item, body, options)); await finish(task, '任务安排已保存。');
      } else if (page === 'knowledge-correct') {
        if (data.correction) {
          confirmed(values); const correction = data.correction;
          const body = { expected_graph_version: data.diff.base_graph_version, reason: reason() };
          await command('confirm-correction', body, options => api.confirmCorrection(id, correction.correction_id, body, options));
          const all = correctionMap(); delete all[id]; storageWrite(storage, CORRECTIONS, all);
          await finish(task, '纠错已发布。请前往“知识更新”确认空间采用的新版本。', { reread: false });
          if (active(task.ticket)) view.data = { ...data, correction: null, correctionPublished: true };
        } else {
          const [kind, targetId] = String(values.get('target') || '').split(':');
          const target = arr(kind === 'relation' ? data.graph.edges : data.graph.nodes).find(row => row.id === targetId);
          const body = correctionPayload(values, target, kind);
          const response = await command(page, body, options => api.createCorrection(id, body, options), { wait: false });
          if (!response.correction_id || !response.candidate_revision_id) throw new Error('未收到有效的纠错候选，请刷新检查。');
          const ref = target.source_refs.find(ref => ref.chunk_id === body.source_ref);
          const descriptor = { ...response, materialId: ref.material_id, body };
          storageWrite(storage, CORRECTIONS, { ...correctionMap(), [id]: descriptor });
          await finish(task, '已创建纠错候选。请核对变更后确认发布。');
        }
      }
    });
  }
  async function filter(values) {
    sync(); if (view.busy) return;
    try {
      view.filters = { topic_id: values.get('topic_id') || '', kind: values.get('kind') || '', status: values.get('status') || '', ...dateRange(values) };
      await load(true);
    } catch (error) { view.error = error.message; changed(); }
  }
  async function action(name, value) {
    if (name === 'reload') return load(true);
    if (name === 'poll-command') {
      sync(); if (view.busy || !view.pendingRun) return;
      const ticket = generation, key = view.key, route = { ...view.route }, runId = view.pendingRun;
      view.busy = '正在核对原任务的处理结果…'; view.error = ''; changed();
      try {
        await api.waitRun(runId);
        for (const [slot, entry] of Object.entries(pending)) if (entry.response?.run_id === runId && JSON.parse(slot)[0] === key) delete pending[slot];
        savePending(); await committed(route, view.data);
        if (route.page === 'material-delete') { if (active(ticket)) navigate('#/materials'); }
        else {
          if (route.page === 'knowledge-correct') { const all = correctionMap(); delete all[route.id]; storageWrite(storage, CORRECTIONS, all); }
          const data = await read(route);
          if (active(ticket)) { view.data = data; view.editRevision++; view.notice = '后台任务已确认完成。'; }
        }
      } catch (error) {
        if (['failed', 'cancelled'].includes(error.details?.runStatus)) {
          for (const [slot, entry] of Object.entries(pending)) if (entry.response?.run_id === runId && JSON.parse(slot)[0] === key) delete pending[slot];
          savePending();
        }
        if (active(ticket)) view.error = error.message;
      } finally { if (active(ticket)) { view.busy = ''; changed(); } }
      return;
    }
    return run('正在处理…', async task => {
      const command = (...args) => executeCommand(task.key, ...args);
      const { page, id, item } = task.route, data = task.data;
      if (name === 'more') {
        const field = page === 'evidence' ? 'evidence' : 'history', previous = data[field];
        if (!previous?.next_cursor) return;
        const result = await (field === 'evidence' ? api.evidence(id, { ...view.filters, cursor: previous.next_cursor }) : api.changes(id, { cursor: previous.next_cursor }));
        if (active(task.ticket)) view.data[field] = { ...result, items: [...arr(previous.items), ...arr(result.items)] };
      } else if (name === 'export') {
        const result = await command(name, {}, options => api.exportSpace(id, options));
        if (!result.export_id) throw new Error('未收到导出文件标识。');
        if (active(task.ticket)) view.data.exportId = result.export_id;
        const response = await api.downloadExport(result.export_id);
        download(await response.blob(), 'KnowPath-' + data.space.name + '.zip');
        if (active(task.ticket)) view.notice = '导出文件已准备好，浏览器已开始下载。';
      } else if (name === 'download') {
        const response = await api.downloadExport(data.exportId); download(await response.blob(), 'KnowPath-' + data.space.name + '.zip');
      } else if (name === 'source') {
        const refs = JSON.parse(value); await openSource(refs, data.space?.id || data.assessment?.space_id);
      } else if (name === 'session-start') {
        if (data.session?.status === 'active') throw new Error('这个空间已有进行中的学习会话，请先结束。');
        if (['completed', 'skipped', 'deferred'].includes(data.task.status)) throw new Error('已完成、跳过或延期的任务不能开始学习，请先调整计划。');
        const result = await command(name, { task_id: item }, options => api.startSession(id, { task_id: item }, options), { wait: false });
        if (!result.session_id || result.status !== 'active') throw new Error('后端未确认开始学习。');
        const descriptor = { ...result, space_id: data.space.id, route: workspacePath('session', id, item), paused: false };
        storageWrite(storage, SESSIONS, { ...sessionMap(), [data.space.id]: descriptor });
        if (active(task.ticket)) { view.data.session = descriptor; view.notice = '学习会话已开始。'; }
      } else if (name === 'session-finish') {
        const session = data.session; if (!session || session.task_id !== item || session.plan_id !== id) throw new Error('请返回正在进行的会话结束学习。');
        const result = await command(name, { session_id: session.session_id }, options => api.finishSession(session.session_id, options), { wait: false });
        if (result.status !== 'finished') throw new Error('后端尚未确认会话结束。');
        const descriptor = { ...session, ...result };
        storageWrite(storage, SESSIONS, { ...sessionMap(), [data.space.id]: descriptor });
        if (active(task.ticket)) { view.data.session = descriptor; view.notice = '会话已保存。可以在任务管理中记录完成情况。'; }
      } else if (name === 'session-event') {
        const session = data.session; if (!session || session.status !== 'active' || session.task_id !== item || session.plan_id !== id) throw new Error('请先开始当前任务的学习会话。');
        if (!['pause', 'resume', 'open_material', 'request_explanation', 'request_hint'].includes(value)) throw new Error('无效的学习操作。');
        const body = { type: value, ...(data.task.topic_ids?.[0] ? { topic_id: data.task.topic_ids[0] } : {}) };
        await command(name, { session_id: session.session_id, ...body }, options => api.sessionEvent(session.session_id, body, options), { wait: false });
        const descriptor = { ...session, paused: value === 'pause' ? true : value === 'resume' ? false : session.paused };
        storageWrite(storage, SESSIONS, { ...sessionMap(), [data.space.id]: descriptor });
        if (active(task.ticket)) { view.data.session = descriptor; view.notice = '学习操作已记录。'; }
        if (value === 'open_material') {
          const topic = sessionTopics(data)[0];
          const ref = topic?.source_refs?.[0]; if (!ref) throw new Error('任务没有可定位的资料来源，请在图谱中选择来源。');
          await openSource(ref, data.space.id);
        } else if (value === 'request_explanation' || value === 'request_hint') {
          if (active(task.ticket)) navigate('#/space/' + encodeURIComponent(data.space.id) + '/assistant', { spaceId: data.space.id,
            learningSessionId: session.session_id, planId: id, taskId: item, returnTo: workspacePath('session', id, item),
            prompt: value === 'request_hint' ? '请针对当前学习主题给我一个提示，不直接给出答案。' : '请解释当前学习主题，并引用资料依据。',
            topicIds: arr(data.task.topic_ids), topics: sessionTopics(data) });
        }
      }
    });
  }
  return { sync, snapshot: () => sync(), load, submit, filter, action };
}
