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
    if (method === 'POST') requestHeaders['Idempotency-Key'] = key || crypto.randomUUID();
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
  async function list(path) {
    const items = [];
    let cursor;
    const seen = new Set();
    do {
      const page = await request(`${path}?limit=100${cursor ? `&cursor=${id(cursor)}` : ''}`);
      if (!Array.isArray(page.items)) throw new ApiError('列表数据格式不正确。');
      items.push(...page.items);
      cursor = page.next_cursor;
      if (cursor && seen.has(cursor)) throw new ApiError('分页游标重复，请刷新后重试。');
      seen.add(cursor);
    } while (cursor);
    return items;
  }
  async function topics(space) {
    const results = await Promise.all((space.bindings || []).map(b => request(`/materials/${id(b.material_id)}/topics?version_id=${id(b.material_version_id)}`)));
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
  async function waitRun(runId, signal) {
    const start = Date.now();
    while (Date.now() - start < 90000) {
      if (signal?.aborted) throw new DOMException('操作已停止', 'AbortError');
      const run = await request(`/runs/${id(runId)}`, { signal });
      if (['succeeded', 'completed'].includes(run.status)) return run;
      if (['failed', 'cancelled'].includes(run.status)) throw new ApiError(run.error?.message || '后台任务未完成，请检查后台服务后重试。');
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
  return { request, list, topics, graph, waitRun, readMessage,
    spaces: () => list('/learning-spaces'), materials: () => list('/materials'),
    space: value => request(`/learning-spaces/${id(value)}`),
    createSpace: body => request('/learning-spaces', { method: 'POST', body }),
    setScope: (space, topicIds) => request(`/learning-spaces/${id(space.id)}/scope`, { method: 'POST', body: { topic_ids: topicIds, include_prerequisites: true, expected_version: space.space_version } }),
    upload: file => { const body = new FormData(); body.set('file', file); body.set('auto_ingest', 'true'); return request('/materials', { method: 'POST', body, timeout: 60000 }); },
    material: value => request(`/materials/${id(value)}`),
    graphDiff: value => request(`/materials/${id(value)}/graph-diff`),
    publish: (materialId, revisionId, version) => request(`/materials/${id(materialId)}/graph-revisions/${id(revisionId)}/publish`, { method: 'POST', body: { expected_graph_version: version, resolutions: [] } }),
    createPlan: (spaceId, body) => request(`/learning-spaces/${id(spaceId)}/plans`, { method: 'POST', body }),
    plan: value => request(`/plans/${id(value)}`),
    completeTask: (plan, task) => request(`/plans/${id(plan.id || plan.plan_id)}/tasks/${id(task.id)}`, { method: 'PATCH', body: { status: 'completed', expected_plan_version: plan.version } }),
    sendMessage: (spaceId, message, sessionId) => request(`/learning-spaces/${id(spaceId)}/messages`, { method: 'POST', body: { message, stream: true, ...(sessionId ? { session_id: sessionId } : {}) } }),
    cancelRun: runId => request(`/runs/${id(runId)}/cancel`, { method: 'POST', body: {} }),
    source: ref => request(`/materials/${id(ref.material_id)}/versions/${id(ref.material_version_id)}/chunks/${id(ref.chunk_id)}`),
  };
}
