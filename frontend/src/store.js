export const DEMO_KEY = 'knowpath-demo-v1';
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
export const MODE_KEY = 'knowpath-mode';
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
const names = [
 ['线性代数', '向量与空间', '线性组合', '矩阵运算', '线性变换', '特征值', '特征向量', '正交与投影'],
 ['Python 编程', '基础语法', '变量与类型', '流程控制', '函数', '面向对象', '文件读写'],
 ['机器学习', '监督学习', '线性回归', '梯度下降', '模型评估', '过拟合与正则化'],
];
export function makeDemo(now = new Date()) {
  const timestamp = now.toISOString();
  const spaces = [
    { id: 'demo-linear', name: '线性代数', goal: '从几何直觉出发，理解数学的语言。', theme: 'blue', material_ids: ['demo-m1', 'demo-m2'], weekly_minutes: 150 },
    { id: 'demo-python', name: 'Python 编程', goal: '让想法落地，从第一行代码开始。', theme: 'sage', material_ids: ['demo-m3'], weekly_minutes: 120 },
    { id: 'demo-ml', name: '机器学习入门', goal: '走近数据背后的规律与可能。', theme: 'lavender', material_ids: ['demo-m4'], weekly_minutes: 90 },
  ].map((s, i) => ({ ...s, status: 'active', created_at: timestamp, topic_ids: names[i].map((_, j) => `${s.id}-t${j}`), space_version: 1, profile_version: 1,
    nodes: names[i].map((label, j) => ({ id: `${s.id}-t${j}`, title: label, status: j > 0 && j < 4 ? 'mastered' : j === 4 ? 'learning' : 'new', description: j === 0 ? s.goal : `从「${label}」出发，梳理概念之间的联系，并通过练习加深理解。`, source_refs: [{ material_id: s.material_ids[0] }] })),
    edges: names[i].slice(1).map((_, j) => ({ id: `${s.id}-e${j}`, from_id: `${s.id}-t${j > 3 ? 3 : 0}`, to_id: `${s.id}-t${j + 1}`, type: 'contains' })),
  }));
  const materials = [
    { id: 'demo-m1', name: '线性代数 · 概念与直觉.md', type: 'md', size_bytes: 24350, text: '# 线性代数：从直觉到理解\n\n## 向量与空间\n向量可以理解为带有方向和大小的量。二维向量 (x, y) 描述平面上的一个点或一次位移。\n\n## 线性组合\n向量 u、v 的线性组合是 a·u + b·v。通过改变系数 a、b，我们得到这两个向量张成的空间。\n\n## 矩阵与线性变换\n矩阵可以描述一种保持向量加法和数乘的变换。矩阵的各列是标准基向量经过变换后的像。\n\n## 特征向量\n某些非零向量经过变换后仍与原方向共线，它们就是特征向量。满足 Av = λv，其中 λ 是对应的特征值。\n\n这是一份内置示例笔记，用于体验前端交互。' },
    { id: 'demo-m2', name: '矩阵运算 · 练习笔记.txt', type: 'txt', size_bytes: 8650, text: '矩阵运算练习\n\n1. 设 A = [[1, 2], [0, 1]]，求向量 (1, 1) 变换后的结果。\n2. 解释单位矩阵为何不会改变向量。\n3. 比较 AB 和 BA：矩阵乘法一定满足交换律吗？\n\n参考思路：先对每一行进行点积，再用坐标变化检查结果。\n\n内置示例内容。' },
    { id: 'demo-m3', name: 'Python 学习手册.md', type: 'md', size_bytes: 32760, text: '# Python 学习手册\n\n## 变量\n变量用于命名和引用一个值。\n\n## 函数\n使用 def 定义可复用的逻辑。参数描述输入，return 描述返回值。\n\ndef greet(name):\n    return f"你好，{name}"\n\n## 练习\n写一个函数，计算列表中所有数字的平均值，并处理空列表。\n\n内置示例内容。' },
    { id: 'demo-m4', name: '机器学习 · 第一课.md', type: 'md', size_bytes: 18620, text: '# 机器学习入门\n\n## 监督学习\n利用带有标签的样本学习输入到输出的映射。\n\n## 线性回归\n用线性函数预测连续数值，通过最小化损失调整参数。\n\n## 梯度下降\n沿着损失函数梯度的反方向逐步更新参数。学习率控制每一步的大小。\n\n## 模型评估\n将训练数据和测试数据分开，以评估对未见样本的泛化能力。\n\n内置示例内容。' },
  ].map(m => ({ ...m, status: 'ready', created_at: timestamp, version: 1 }));
  const tasks = [
    { id: 'demo-task-1', space_id: 'demo-linear', title: '理解矩阵与线性变换', type: 'learn', minutes: 25, status: 'pending', topic_ids: ['demo-linear-t4'] },
    { id: 'demo-task-2', space_id: 'demo-python', title: '温习函数与参数', type: 'review', minutes: 15, status: 'pending', topic_ids: ['demo-python-t4'] },
    { id: 'demo-task-3', space_id: 'demo-linear', title: '完成向量的线性组合练习', type: 'practice', minutes: 10, status: 'completed', topic_ids: ['demo-linear-t2'] },
  ];
  return { version: 1, spaces, materials, tasks };
}
export function loadDemo(storage) {
  const value = storageRead(storage, DEMO_KEY);
  if (value?.version === 1 && Array.isArray(value.spaces) && Array.isArray(value.materials) && Array.isArray(value.tasks)
    && value.spaces.every(s => typeof s.id === 'string' && typeof s.name === 'string' && Array.isArray(s.nodes) && Array.isArray(s.edges))
    && value.materials.every(m => typeof m.id === 'string' && typeof m.name === 'string')
    && value.tasks.every(t => typeof t.id === 'string' && typeof t.title === 'string')) return value;
  return makeDemo();
}
export function spaceProgress(space) {
  const nodes = space.nodes || [];
  if (!nodes.length) return null;
  return Math.round(nodes.filter(n => n.status === 'mastered').length / nodes.length * 100);
}
export function materialIds(space) { return space.material_ids || (space.bindings || []).map(b => b.material_id); }

export function removeSpaceData(data, spaceId) {
  data.spaces = data.spaces.filter(space => space.id !== spaceId);
  data.tasks = data.tasks.filter(task => task.space_id !== spaceId);
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
