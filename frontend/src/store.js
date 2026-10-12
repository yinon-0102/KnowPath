export const graphTitle = node => String(node?.title || node?.name || '未命名知识点').replace(/\s+/g, ' ').trim();

export function buildGraphIndex(graph = {}) {
  const nodes = [], byId = new Map();
  for (const node of graph.nodes || []) {
    if (!node?.id || byId.has(node.id)) continue;
    byId.set(node.id, node); nodes.push(node);
  }
  const edges = [], seen = new Set(), declaredParents = new Set();
  const add = edge => {
    if (!byId.has(edge.from_id) || !byId.has(edge.to_id) || edge.from_id === edge.to_id) return;
    const key = JSON.stringify([edge.from_id, edge.to_id, edge.type]);
    if (seen.has(key)) return;
    seen.add(key); edges.push(edge);
  };
  for (const edge of graph.edges || []) {
    if (edge.type === 'contains') declaredParents.add(JSON.stringify([edge.from_id, edge.to_id]));
    if (!['rejected', 'superseded', 'inactive'].includes(edge.status)) add(edge);
  }
  // parent_id is a supplied document hierarchy, never a semantic relationship guessed from text.
  for (const node of nodes) {
    if (node.parent_id && !declaredParents.has(JSON.stringify([node.parent_id, node.id]))) {
      add({ from_id: node.parent_id, to_id: node.id, type: 'contains', structural: true });
    }
  }
  const adjacent = new Map(nodes.map(node => [node.id, new Map()]));
  const children = new Set();
  for (const edge of edges) {
    if (edge.type === 'contains') children.add(edge.to_id);
    for (const [from, to] of [[edge.from_id, edge.to_id], [edge.to_id, edge.from_id]]) {
      const list = adjacent.get(from);
      if (!list.has(to)) list.set(to, { node: byId.get(to), edges: [] });
      list.get(to).edges.push(edge);
    }
  }
  const roots = nodes.filter(node => !children.has(node.id));
  return { nodes, byId, edges, adjacent, roots: roots.length ? roots : nodes };
}

export function graphRelation(edge, focusId) {
  const outgoing = edge.from_id === focusId;
  let label = ({
    contains: outgoing ? '下级主题' : '上级主题',
    prerequisite_of: outgoing ? '后续知识' : '前置知识',
    related_to: '相关知识',
    assessed_by: outgoing ? '测评依据' : '测评对象',
    explained_by: outgoing ? '解释依据' : '解释对象',
    supersedes: outgoing ? '替代的知识' : '更新版本',
    contradicts: '存在冲突',
  })[edge.type] || '关联知识';
  if (edge.status && edge.status !== 'active') label += '（待确认）';
  return label;
}

export function graphSlice(index, { focusId = '', query = '', page = 0, scope = 'overview', relation = 'all' } = {}) {
  const search = String(query).trim().toLocaleLowerCase();
  const focus = !search ? index.byId.get(focusId) : null;
  let entries;
  if (focus) {
    entries = [...index.adjacent.get(focus.id).values()].map(entry => {
      const edges = entry.edges.filter(edge => relation === 'all'
        || relation === 'children' && edge.type === 'contains' && edge.from_id === focus.id
        || relation === 'parents' && edge.type === 'contains' && edge.to_id === focus.id
        || relation === 'prerequisites' && edge.type === 'prerequisite_of' && edge.to_id === focus.id
        || relation === 'other' && edge.type !== 'contains');
      return { ...entry, edges, labels: [...new Set(edges.map(edge => graphRelation(edge, focus.id)))] };
    }).filter(entry => entry.edges.length);
    entries.sort((a, b) => {
      const order = item => item.edges.some(e => e.type === 'contains' && e.from_id === focus.id) ? 0 : item.edges.some(e => e.type === 'contains') ? 1 : 2;
      return order(a) - order(b);
    });
  } else {
    const candidates = search || scope === 'all' ? index.nodes : index.roots;
    entries = candidates.filter(node => !search || (graphTitle(node) + ' ' + (node.description || '')).toLocaleLowerCase().includes(search)).map(node => ({ node, edges: [], labels: [] }));
  }
  const pageSize = focus ? 4 : 8;
  const pages = Math.max(1, Math.ceil(entries.length / pageSize));
  const current = Math.max(0, Math.min(Number.isFinite(Number(page)) ? Math.floor(Number(page)) : 0, pages - 1));
  return { focus, search, total: entries.length, pages, page: current, pageSize, items: entries.slice(current * pageSize, (current + 1) * pageSize) };
}
export const TOKEN_KEY = 'knowpath-session-token';
export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
export const uid = () => globalThis.crypto.randomUUID();
export const dateText = date => {
  const value = new Date(date);
  return Number.isNaN(value.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(value);
};
export const sizeText = bytes => !Number.isFinite(bytes) ? '—' : bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
export function storageRead(storage, key, fallback = null) {
  try { const raw = storage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; }
}
export function storageWrite(storage, key, value) {
  try { storage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}
export function validateFile(file) {
  if (!/\.(pdf|md|txt)$/i.test(file.name)) throw new Error('请选择 PDF、Markdown（.md）或 TXT 文件。');
  if (file.size > 20 * 1024 * 1024) throw new Error('单个文件不能超过 20 MB。');
  if (file.size === 0) throw new Error('这个文件是空的，请选择有内容的资料。');
}
export function materialIds(space) { return space.material_ids || (space.bindings || []).map(b => b.material_id); }

export function removeSpaceData(data, spaceId) {
  data.spaces = data.spaces.filter(space => space.id !== spaceId);
  data.tasks = data.tasks.filter(task => task.space_id !== spaceId);
  if (Array.isArray(data.studyPlans)) data.studyPlans = data.studyPlans.filter(plan => plan.space_id !== spaceId);
  // Materials are shared; deleting a space must never remove its source files.
}

// PDF copies belong to an exact material version and stay in this browser.
export const pdfKey = (mode, materialId, versionId) => JSON.stringify([mode, materialId, versionId]);
export function sourcePage(source) {
  const pages = [source?.page, ...(source?.source_spans || []).map(span => span.page)];
  return pages.find(page => Number.isInteger(page) && page > 0) || null;
}
export function pdfLocation(url, source, verified = true) {
  const page = verified ? sourcePage(source) : null;
  return url + (page ? `#page=${page}&view=FitH` : '#view=FitH');
}
export async function validatePdf(file, expectedHash = '') {
  if (!file || !/\.pdf$/i.test(file.name || '')) throw new Error('请选择 PDF 文件。');
  if (!file.size || file.size > 20 * 1024 * 1024) throw new Error('PDF 文件需要大于 0 字节且不超过 20 MB。');
  const bytes = await file.arrayBuffer();
  if (!new TextDecoder().decode(bytes.slice(0, 1024)).includes('%PDF-')) throw new Error('这个文件不是有效的 PDF，请重新选择。');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  if (expectedHash && hash !== expectedHash.toLowerCase()) throw new Error('这个 PDF 与知识来源的资料版本不一致，请选择当时导入的原文件。');
  return hash;
}
export function createPdfLibrary(database) {
  if (arguments.length === 0) {
    try { database = globalThis.indexedDB; } catch { database = null; }
  }
  const records = new Map();
  let db;
  const ready = new Promise(resolve => {
    if (!database) return resolve();
    let request;
    try { request = database.open('knowpath-local-pdfs', 1); } catch { resolve(); return; }
    request.onupgradeneeded = () => request.result.createObjectStore('files', { keyPath: 'key' });
    request.onerror = request.onblocked = () => resolve();
    request.onsuccess = () => {
      db = request.result;
      db.onversionchange = () => { db.close(); db = null; };
      try {
        const read = db.transaction('files').objectStore('files').getAll();
        read.onsuccess = () => { for (const item of read.result) records.set(item.key, item); resolve(); };
        read.onerror = () => resolve();
      } catch { resolve(); }
    };
  });
  return {
    ready,
    peek: key => records.get(key),
    get: async key => { await ready; return records.get(key); },
    async save({ key, file, hash, verified }) {
      await ready;
      const record = { key, blob: file.slice(0, file.size, 'application/pdf'), name: file.name, hash, verified };
      records.set(key, record);
      if (!db) return false;
      return new Promise(resolve => {
        try {
          const transaction = db.transaction('files', 'readwrite');
          transaction.objectStore('files').put(record);
          transaction.oncomplete = () => resolve(true);
          transaction.onerror = transaction.onabort = () => resolve(false);
        } catch { resolve(false); }
      });
    },
  };
}
