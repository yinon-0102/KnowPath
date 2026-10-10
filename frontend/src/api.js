// All live calls follow py/knowpath_backend/learning/api contracts.
// Tokens stay in sessionStorage and are sent only to the fixed local API.
export const API_BASE = 'http://127.0.0.1:8000/api/v1';
export class ApiError extends Error {
  constructor(message, status = 0, code = '', details = {}) { super(message); this.status = status; this.code = code; this.details = details; }
}
export function createApi(getToken) {
  async function request(path, { method = 'GET', body, signal, timeout = 20000, headers = {}, raw = false, key } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => controller.abort(new Error('请求超时')), timeout);
    const isForm = body instanceof FormData;
    const token = getToken();
    const requestHeaders = { ...(token ? { 'X-Local-Token': token } : {}), ...headers };
    if (body != null && !isForm) requestHeaders['Content-Type'] = 'application/json';
    if (method === 'POST' || key) requestHeaders['Idempotency-Key'] = key || crypto.randomUUID();
    try {
      const response = await fetch(API_BASE + path, { method, headers: requestHeaders, body: body == null ? undefined : isForm ? body : JSON.stringify(body), signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const error = payload.error || {};
        const message = response.status === 401 ? '本地会话令牌无效，请打开连接设置重新输入。' : error.message || `请求失败（${response.status}），请重试。`;
        throw new ApiError(message, response.status, error.code, error.details);
      }
      if (raw) return response;
      return response.status === 204 ? null : await response.json();
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (signal?.aborted) throw new DOMException('操作已停止', 'AbortError');
      if (controller.signal.aborted) throw new ApiError('请求超时，请检查本地服务后重试。', 0, 'TIMEOUT');
      throw new ApiError('无法连接本地服务，请确认后端已在 127.0.0.1:8000 启动。', 0, 'NETWORK');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  }
  const id = encodeURIComponent;
  const query = (path, params = {}) => {
    const values = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value !== '' && value != null) values.set(key, String(value));
    return path + (values.size ? (path.includes('?') ? '&' : '?') + values : '');
  };
  async function list(path, params = {}, options = {}) {
    const items = [];
    let cursor;
    const seen = new Set();
    do {
      const page = await request(query(path, { ...params, limit: 100, cursor }), options);
      if (!Array.isArray(page.items)) throw new ApiError('列表数据格式不正确。');
      items.push(...page.items);
      cursor = page.next_cursor;
      if (cursor && seen.has(cursor)) throw new ApiError('分页游标重复，请刷新后重试。');
      seen.add(cursor);
    } while (cursor);
    return items;
  }
  async function topics(space, options = {}) {
    const results = await Promise.all((space.bindings || []).map(async b => {
      const result = await request(`/materials/${id(b.material_id)}/topics?version_id=${id(b.material_version_id)}`, options);
      if (result.material_id !== b.material_id || result.version_id !== b.material_version_id
          || (b.graph_version != null && result.graph_version !== b.graph_version)) {
        throw new ApiError('知识版本与学习空间绑定不一致，请先在学习空间确认知识更新。', 409, 'STALE_KNOWLEDGE');
      }
      if (!Array.isArray(result.items)) throw new ApiError('知识主题返回格式不正确。');
      return result;
    }));
    return results.flatMap(result => result.items || []);
  }
  async function graph(space) {
    const allTopics = await topics(space);
    const permitted = new Set(allTopics.map(t => t.id));
    const roots = [...new Set([...(space.topic_ids || []), ...allTopics.filter(t => t.level === 1).map(t => t.id)])].filter(t => permitted.has(t));
    if (!roots.length && allTopics.length) roots.push(allTopics[0].id);
    const nodes = new Map(allTopics.map(t => [t.id, t]));
    const edges = new Map();
    for (const root of roots) {
      const data = await request(`/topics/${id(root)}/graph?depth=3&include_sources=true`);
      for (const node of data.nodes || []) if (permitted.has(node.id)) {
        const bound = nodes.get(node.id);
        if (bound.graph_version !== node.graph_version || bound.material_version_id !== node.material_version_id) throw new ApiError('图谱版本与空间绑定的快照不同，请确认知识版本后重试。');
        nodes.set(node.id, node);
      }
      for (const edge of data.edges || []) if (permitted.has(edge.from_id) && permitted.has(edge.to_id)) edges.set(edge.id, edge);
    }
    return { nodes: [...nodes.values()], edges: [...edges.values()] };
  }
  async function materialGraph(material, options = {}) {
    const versionId = material?.current_version_id || material?.version_id;
    const result = await request(`/materials/${id(material.id)}/topics${versionId ? `?version_id=${id(versionId)}` : ''}`, options);
    if (!Array.isArray(result.items)) throw new ApiError('资料知识主题返回格式不正确。');
    const permitted = new Set(result.items.map(topic => topic.id));
    const nodes = new Map(result.items.map(topic => [topic.id, topic]));
    const edges = new Map();
    const addEdge = edge => {
      if (!edge?.from_id || !edge?.to_id || !permitted.has(edge.from_id) || !permitted.has(edge.to_id)) return;
      const key = `${edge.type || 'related_to'}:${edge.from_id}:${edge.to_id}`;
      edges.set(key, edge.id ? edge : { ...edge, id: key });
    };
    for (const topic of result.items) {
      if (topic.parent_id) addEdge({ id: `contains:${topic.parent_id}:${topic.id}`, type: 'contains', from_id: topic.parent_id, to_id: topic.id, source_refs: topic.source_refs || [] });
      for (const prerequisite of [...(topic.prerequisites || []), ...(topic.recommended_prerequisites || [])]) {
        const source = typeof prerequisite === 'string' ? prerequisite : prerequisite?.id;
        if (source) addEdge({ id: `prerequisite_of:${source}:${topic.id}`, type: 'prerequisite_of', from_id: source, to_id: topic.id, source_refs: topic.source_refs || [] });
      }
    }
    const roots = result.items.filter(topic => !topic.parent_id).map(topic => topic.id);
    for (const root of roots.length ? roots : result.items.slice(0, 1).map(topic => topic.id)) {
      const data = await request(`/topics/${id(root)}/graph?depth=3&include_sources=true`, options);
      for (const node of data.nodes || []) if (permitted.has(node.id)) nodes.set(node.id, node);
      for (const edge of data.edges || []) addEdge(edge);
    }
    return { nodes: [...nodes.values()], edges: [...edges.values()], graph_version: result.graph_version, material_id: result.material_id, material_version_id: result.version_id };
  }
  async function waitRun(runId, signal) {
    const start = Date.now();
    while (Date.now() - start < 90000) {
      if (signal?.aborted) throw new DOMException('操作已停止', 'AbortError');
      const run = await request(`/runs/${id(runId)}`, { signal });
      if (['succeeded', 'completed'].includes(run.status)) return run;
      if (['failed', 'cancelled'].includes(run.status)) {
        throw new ApiError(run.error?.message || '后台任务未完成，请重试。', 200,
          run.error?.code || (run.status === 'cancelled' ? 'RUN_CANCELLED' : 'RUN_FAILED'),
          { ...run.error?.details, runId, runStatus: run.status });
      }
      await new Promise(resolve => setTimeout(resolve, 900));
    }
    throw new ApiError('后台仍在处理。稍后刷新查看结果。', 0, 'RUN_PENDING', { runId });
  }
  async function readMessage(job, { signal, onStatus = () => {} } = {}) {
    // Keep the same run and cursor on reconnection: never resubmit the question.
    job.lastEventId ||= '0';
    for (let attempt = 0; attempt < 3; attempt++) {
      if (signal?.aborted) throw new DOMException('操作已停止', 'AbortError');
      let reader;
      const controller = new AbortController();
      const abort = () => { controller.abort(); if (reader) void reader.cancel().catch(() => {}); };
      signal?.addEventListener('abort', abort, { once: true });
      const timeout = setTimeout(abort, 90000);
      try {
        onStatus(attempt ? '正在重新连接回答…' : '正在检索资料与生成回答…');
        const response = await request(`/runs/${id(job.run_id)}/events`, { raw: true, signal: controller.signal, headers: { Accept: 'text/event-stream', 'Last-Event-ID': job.lastEventId } });
        if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) throw new ApiError('后端未返回正确的事件流。');
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '', frame = { event: '', data: [], id: '' }, completed = false;
        const dispatch = () => {
          const event = frame; frame = { event: '', data: [], id: '' };
          if (!event.data.length) return;
          if (/^\d+$/.test(event.id) && BigInt(event.id) <= BigInt(job.lastEventId)) return;
          const data = JSON.parse(event.data.join('\n'));
          if (/^\d+$/.test(event.id)) job.lastEventId = event.id;
          if (event.event === 'message.completed') job.result = data;
          if (event.event === 'run.completed') completed = true;
          if (event.event === 'run.failed') throw new ApiError(data.error?.message || '回答生成失败，请检查模型配置。', 400, 'RUN_FAILED');
          if (event.event === 'run.cancelled') throw new ApiError('任务已取消。', 400, 'RUN_CANCELLED');
        };
        while (!completed) {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          let newline;
          while ((newline = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, newline).replace(/\r$/, ''); buffer = buffer.slice(newline + 1);
            if (!line) { dispatch(); continue; }
            if (line.startsWith(':')) continue;
            const colon = line.indexOf(':');
            const field = colon < 0 ? line : line.slice(0, colon);
            const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
            if (field === 'data') frame.data.push(value);
            else if (field === 'id' || field === 'event') frame[field] = value;
          }
          if (done) { dispatch(); break; }
        }
        if (job.result && typeof job.result.text === 'string') return job.result;
        if (completed) throw new ApiError('任务完成但未返回回答正文。', 400, 'MISSING_RESULT');
      } catch (error) {
        if (signal?.aborted) throw new DOMException('操作已停止', 'AbortError');
        if (error instanceof ApiError && error.status >= 400) throw error;
        if (attempt === 2) throw new ApiError('回答连接中断。点击“继续接收”可恢复同一个任务。', 0, 'RUN_PENDING');
      } finally { clearTimeout(timeout); signal?.removeEventListener('abort', abort); if (reader) await reader.cancel().catch(() => {}); }
    }
    throw new ApiError('后台仍在生成，点击“继续接收”查看结果。', 0, 'RUN_PENDING');
  }
  return { request, list, topics, graph, materialGraph, waitRun, readMessage,
    createAssessment: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/assessments`, { ...options, method: 'POST', body }),
    assessment: (assessmentId, options = {}) => request(`/assessments/${id(assessmentId)}`, options),
    diagnostic: (assessmentId, options = {}) => request(`/assessments/${id(assessmentId)}/diagnostic`, options),
    recordAttempt: (assessmentId, body, options = {}) => request(`/assessments/${id(assessmentId)}/attempts`, { ...options, method: 'POST', body }),
    finalizeAssessment: (assessmentId, body, options = {}) => request(`/assessments/${id(assessmentId)}/finalize`, { ...options, method: 'POST', body }),
    assessmentResult: (assessmentId, options = {}) => request(`/assessments/${id(assessmentId)}/result`, options),
    evolution: (spaceId, options = {}) => request(`/learning-spaces/${id(spaceId)}/evolution`, options),
    comparePlans: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/plan-comparisons`, { ...options, method: 'POST', body }),
    replayPolicies: (spaceId, body = {}, options = {}) => request(`/learning-spaces/${id(spaceId)}/policy-replays`, { ...options, method: 'POST', body }),
    spaces: () => list('/learning-spaces'), materials: () => list('/materials'),
    space: (value, options = {}) => request(`/learning-spaces/${id(value)}`, options),
    createSpace: body => request('/learning-spaces', { method: 'POST', body }),
    setSpaceStatus: (space, status, options = {}) => request(`/learning-spaces/${id(space.id)}`, { ...options, method: 'PATCH', body: { status, expected_version: space.space_version } }),
    deleteSpace: (space, options = {}) => request(`/learning-spaces/${id(space.id)}`, { ...options, method: 'DELETE', body: { confirm: true, expected_version: space.space_version } }),
    setScope: (space, topicIds, excluded = [], includePrerequisites = true, options = {}) => request(`/learning-spaces/${id(space.id)}/scope`, { ...options, method: 'POST', body: { topic_ids: topicIds, excluded_topic_ids: excluded, include_prerequisites: includePrerequisites, expected_version: space.space_version } }),
    upload: file => { const body = new FormData(); body.set('file', file); body.set('auto_ingest', 'true'); return request('/materials', { method: 'POST', body, timeout: 60000 }); },
    material: (value, spaceId) => request(`/materials/${id(value)}${spaceId ? '?space_id=' + id(spaceId) : ''}`),
    materialVersions: value => list(`/materials/${id(value)}/versions`),
    graphDiff: (value, revisionId, options = {}) => request(query(`/materials/${id(value)}/graph-diff`, { revision_id: revisionId }), options),
    publish: (materialId, revisionId, version, resolutions = [], options = {}) => request(`/materials/${id(materialId)}/graph-revisions/${id(revisionId)}/publish`, { ...options, method: 'POST', body: { expected_graph_version: version, resolutions } }),
    createPlan: (spaceId, body) => request(`/learning-spaces/${id(spaceId)}/plans`, { method: 'POST', body }),
    plan: value => request(`/plans/${id(value)}`),
    completeTask: (plan, task) => request(`/plans/${id(plan.id || plan.plan_id)}/tasks/${id(task.id)}`, { method: 'PATCH', body: { status: 'completed', expected_plan_version: plan.version } }),
    sendMessage: (spaceId, message, sessionId, materialIds = []) => request(`/learning-spaces/${id(spaceId)}/messages`, { method: 'POST', body: { message, stream: true, ...(sessionId ? { session_id: sessionId } : {}), ...(materialIds.length ? { material_ids: materialIds } : {}) } }),
    updateMaterial: (materialId, body, options = {}) => request(`/materials/${id(materialId)}`, { ...options, method: 'PATCH', body }),
    deleteMaterial: (materialId, body, options = {}) => request(`/materials/${id(materialId)}`, { ...options, method: 'DELETE', body }),
    uploadVersion: (materialId, file, note, options = {}) => {
      const body = new FormData(); body.set('file', file); body.set('auto_ingest', 'true');
      if (note) body.set('change_note', note);
      return request(`/materials/${id(materialId)}/versions`, { ...options, method: 'POST', body, timeout: 60000 });
    },
    ingest: (materialId, body, options = {}) => request(`/materials/${id(materialId)}/ingest`, { ...options, method: 'POST', body }),
    reconcile: (materialId, body, options = {}) => request(`/materials/${id(materialId)}/reconcile`, { ...options, method: 'POST', body }),
    updateSpace: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}`, { ...options, method: 'PATCH', body }),
    profile: (spaceId, options = {}) => request(`/learning-spaces/${id(spaceId)}/profile`, options),
    updateProfile: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/profile`, { ...options, method: 'PATCH', body }),
    learningState: (spaceId, params = {}, options = {}) => request(query(`/learning-spaces/${id(spaceId)}/state`, params), options),
    resetState: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/state/reset`, { ...options, method: 'POST', body }),
    evidence: (spaceId, params = {}, options = {}) => request(query(`/learning-spaces/${id(spaceId)}/evidence`, { limit: 20, ...params }), options),
    changes: (spaceId, params = {}, options = {}) => request(query(`/learning-spaces/${id(spaceId)}/changes`, { limit: 20, ...params }), options),
    knowledgeUpdates: (spaceId, options = {}) => request(`/learning-spaces/${id(spaceId)}/knowledge-updates`, options),
    applyKnowledgeUpdates: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/knowledge-updates/apply`, { ...options, method: 'POST', body }),
    createCorrection: (spaceId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/knowledge-corrections`, { ...options, method: 'POST', body }),
    confirmCorrection: (spaceId, correctionId, body, options = {}) => request(`/learning-spaces/${id(spaceId)}/knowledge-corrections/${id(correctionId)}/confirm`, { ...options, method: 'POST', body }),
    gradeReview: (assessmentId, body, options = {}) => request(`/assessments/${id(assessmentId)}/grade-reviews`, { ...options, method: 'POST', body }),
    questionReviews: (assessmentId, options = {}) => request(`/assessments/${id(assessmentId)}/question-reviews`, options),
    reportQuestion: (assessmentId, body, options = {}) => request(`/assessments/${id(assessmentId)}/question-reviews`, { ...options, method: 'POST', body }),
    resolveQuestion: (assessmentId, reviewId, body, options = {}) => request(`/assessments/${id(assessmentId)}/question-reviews/${id(reviewId)}/resolve`, { ...options, method: 'POST', body }),
    updateTask: (planId, taskId, body, options = {}) => request(`/plans/${id(planId)}/tasks/${id(taskId)}`, { ...options, method: 'PATCH', body }),
    startSession: (planId, body, options = {}) => request(`/plans/${id(planId)}/sessions`, { ...options, method: 'POST', body }),
    sessionEvent: (sessionId, body, options = {}) => request(`/sessions/${id(sessionId)}/events`, { ...options, method: 'POST', body }),
    finishSession: (sessionId, options = {}) => request(`/sessions/${id(sessionId)}/finish`, { ...options, method: 'POST', body: {} }),
    exportSpace: (spaceId, options = {}) => request(`/learning-spaces/${id(spaceId)}/exports`, { ...options, method: 'POST', body: { format: 'json' } }),
    downloadExport: (exportId, options = {}) => request(`/exports/${id(exportId)}/download`, { ...options, raw: true }),
    health: (options = {}) => request('/health', options),
    cancelRun: runId => request(`/runs/${id(runId)}/cancel`, { method: 'POST', body: {} }),
    source: (ref, spaceId) => {
      if (!spaceId) throw new ApiError('请先选择来源所属的学习空间。', 0, 'SPACE_CONTEXT_REQUIRED');
      if (!ref?.material_id || !ref.material_version_id || !ref.chunk_id) throw new ApiError('来源定位信息不完整，请重新加载知识图谱。', 0, 'INVALID_SOURCE');
      if (ref.citation_schema_version === 2) {
        const { citation_schema_version, material_id, material_version_id, retrieval_version_id, chunk_id, source_spans } = ref;
        return request(`/learning-spaces/${id(spaceId)}/citations/resolve`, { method: 'POST', body: { citation_schema_version, material_id, material_version_id, retrieval_version_id, chunk_id, source_spans } });
      }
      if (ref.citation_schema_version != null && ref.citation_schema_version !== 1) throw new ApiError('暂不支持这个来源版本，请更新前端。', 0, 'INVALID_SOURCE');
      return request(`/materials/${id(ref.material_id)}/versions/${id(ref.material_version_id)}/chunks/${id(ref.chunk_id)}?space_id=${id(spaceId)}`);
    },
    materialSource: ref => {
      if (!ref?.material_id || !ref.material_version_id || !ref.chunk_id) throw new ApiError('来源定位信息不完整，请重新加载知识图谱。', 0, 'INVALID_SOURCE');
      return request(`/materials/${id(ref.material_id)}/versions/${id(ref.material_version_id)}/chunks/${id(ref.chunk_id)}`);
    },
  };
}

// State is updated by the caller only after a confirmed successful operation.
export async function changeSpace({ api, mode, space, action, confirmed = false }) {
  if (!space?.id || !Number.isInteger(space.space_version) || space.space_version < 1) {
    throw new Error('空间信息已失效，请刷新页面后重新操作。');
  }
  if (!['archive', 'restore', 'delete'].includes(action)) throw new Error('不支持的空间操作。');
  if (action === 'delete' && confirmed !== true) throw new Error('请先确认删除此空间及其学习记录。');
  const status = action === 'archive' ? 'archived' : 'active';
  if (mode === 'demo') return action === 'delete' ? null : { ...space, status, space_version: space.space_version + 1 };
  if (mode !== 'live') throw new Error('当前数据模式不可用，请刷新页面。');
  if (action !== 'delete') {
    const result = await api.setSpaceStatus(space, status);
    if (result?.id !== space.id || result.status !== status || !Number.isInteger(result.space_version) || result.space_version <= space.space_version) {
      throw new Error('未收到有效的空间状态，请刷新列表核对，勿重复提交。');
    }
    return result;
  }
  const result = await api.deleteSpace(space);
  if (result?.space_id !== space.id) throw new Error('删除响应与当前空间不一致，请刷新列表核对。');
  if (result.status === 'succeeded') return null;
  if (['queued', 'pending', 'running'].includes(result.status) && result.run_id) {
    await api.waitRun(result.run_id);
    return null;
  }
  throw new Error('后端尚未确认删除完成，请刷新列表核对。');
}
