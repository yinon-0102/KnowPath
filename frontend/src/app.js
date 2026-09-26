import { icon, logo } from './icons.js';
import { createApi } from './api.js';
import { DEMO_KEY, MODE_KEY, TOKEN_KEY, esc, uid, dateText, sizeText, storageRead, storageWrite, validateFile, loadDemo, makeDemo, spaceProgress, materialIds } from './store.js';

const $ = (selector, root = document) => root.querySelector(selector);
const main = $('#main');
const modal = $('#modal');
let local, session;
try { local = window.localStorage; } catch { local = { getItem() {}, setItem() { throw new Error('浏览器未允许本地存储'); } }; }
try { session = window.sessionStorage; } catch { session = { getItem() {}, setItem() {} }; }
const fragment = new URLSearchParams(location.hash.slice(1));
const suppliedToken = fragment.get('local_token');
let token = suppliedToken || storageRead(session, TOKEN_KEY, '');
if (suppliedToken) { storageWrite(session, TOKEN_KEY, suppliedToken); history.replaceState(null, '', location.pathname + location.search + '#/overview'); }
const api = createApi(() => token);
const demo = loadDemo(local);
const state = {
  mode: suppliedToken ? 'live' : storageRead(session, MODE_KEY, 'demo'),
  live: { spaces: [], materials: [], tasks: [] }, loading: false, error: '',
  selected: '', spaceFilter: 'all', materialFilter: 'all', query: '',
  graph: null, graphSpace: '', graphError: '', graphLoading: false, graphSelected: '', zoom: 1, pan: { x: 0, y: 0 },
  topics: new Map(), plans: new Map(), planLoading: false, planError: '',
  messages: new Map(), chats: new Map(), pendingJob: null, sending: false, chatController: null,
  timerRemaining: 25 * 60, timerEnd: null, timerInterval: null, timerDuration: 25,
};
const routes = [ ['overview', '概览'], ['spaces', '学习空间'], ['materials', '资料库'], ['graph', '知识图谱'], ['plan', '学习计划'] ];
const statusLabels = { ready: '已就绪', uploaded: '待解析', processing: '解析中', needs_review: '待审核', failed: '解析失败', archived: '已归档', active: '学习中', draft: '待选择范围' };
const taskLabels = { learn: '学习', learning: '学习', review: '复习', practice: '练习', assessment: '测评', retest: '复测' };
const data = () => state.mode === 'demo' ? demo : state.live;
const route = () => {
  const hash = location.hash.slice(1);
  const parts = (hash.startsWith('/') ? hash.slice(1) : hash).split('/');
  let id = '';
  try { id = decodeURIComponent(parts.slice(1).join('/')); } catch { /* Invalid URLs render the missing-space state. */ }
  return { name: parts[0] || 'overview', id };
};
const currentSpace = () => data().spaces.find(s => s.id === state.selected) || data().spaces[0];
const byId = id => data().spaces.find(s => s.id === id);
const activeTasks = () => state.mode === 'demo' ? demo.tasks : [...state.plans.values()].flatMap(p => (p.tasks || []).map(t => ({ ...t, space_id: p.space_id, plan_id: p.id || p.plan_id })));
const link = (label, href, cls = 'text-link', glyph = 'chevron') => `<a class="${cls}" href="${href}">${label}${icon(glyph)}</a>`;
const button = (label, action, cls = 'button button-primary', glyph = '', attrs = '') => `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${glyph ? icon(glyph) : ''}${label}</button>`;
const empty = (title, description, action = '', glyph = 'book') => `<div class="empty-state">${icon(glyph)}<h2>${esc(title)}</h2><p>${esc(description)}</p>${action}</div>`;
const notice = (text, style = '') => `<div class="notice ${style}">${icon('help')}<span>${text}</span></div>`;
let toastTimeout, renderVersion = 0, modalVersion = 0;

function toast(message, error = false) {
  const element = $('#toast'); element.textContent = message; element.className = `toast${error ? ' error' : ''}`; element.hidden = false;
  clearTimeout(toastTimeout); toastTimeout = setTimeout(() => { element.hidden = true; }, error ? 6500 : 3600);
}
function saveDemo() { if (!storageWrite(local, DEMO_KEY, demo)) toast('浏览器存储空间不足，本次修改仅保留到页面关闭。', true); }
function go(name, id = '') { const target = `#/${name}${id ? '/' + encodeURIComponent(id) : ''}`; if (location.hash === target) render(); else location.hash = target; }
function openModal(title, body, className = '') {
  modalVersion++; modal.className = className;
  modal.innerHTML = `<div class="modal-head"><h2 id="modal-title">${title}</h2>${button('', 'close-modal', 'icon-button', 'close', 'aria-label="关闭弹窗"')}</div><div class="modal-body">${body}</div>`;
  if (!modal.open) modal.showModal();
  const focusTarget = $('input:not([type=checkbox]), textarea, select', modal);
  if (focusTarget) focusTarget.focus();
}
function closeModal() { modalVersion++; modal.close(); }
function showError(error, root = modal) {
  const box = $('[data-form-error]', root);
  if (box) { box.textContent = error.message || String(error); box.hidden = false; }
  else toast(error.message || String(error), true);
}
async function busy(element, work) {
  if (element?.disabled) return;
  if (element) { element.disabled = true; element.setAttribute('aria-busy', 'true'); }
  try { return await work(); } catch (error) { if (error.name !== 'AbortError') showError(error); }
  finally { if (element) { element.disabled = false; element.removeAttribute('aria-busy'); } }
}
function header() {
  const current = route().name;
  $('#header').innerHTML = `<div class="container header-inner"><a href="#/overview" class="brand" aria-label="KnowPath 首页">${logo}<span>KnowPath</span></a><nav class="main-nav" id="main-nav" aria-label="主导航">${routes.map(([name, label]) => `<a href="#/${name}" class="nav-link ${current === name || current === 'space' && name === 'spaces' ? 'active' : ''}" ${current === name || current === 'space' && name === 'spaces' ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav><div class="header-actions">${button('', 'search', 'icon-button', 'search', 'aria-label="搜索学习空间和资料" title="搜索（Ctrl / ⌘ K）"')}${button('', 'settings', 'icon-button settings-button', 'settings', 'aria-label="连接与偏好设置"')}${button('我', 'settings', 'avatar', '', 'aria-label="我的设置"')}${button('', 'mobile-menu', 'icon-button mobile-menu', 'menu', 'aria-label="展开导航" aria-expanded="false" aria-controls="main-nav"')}</div></div>`;
  $('#footer').innerHTML = `<div class="container footer-inner"><div class="footer-brand">${logo}<span>KnowPath · 让知识连接，让学习发生。</span></div><div class="footer-links"><span>${state.mode === 'demo' ? '示例数据 · 仅保存在此浏览器' : '本地服务 · 真实学习数据'}</span><button data-action="help">使用指南</button><button data-action="privacy">数据与隐私</button><span>© ${new Date().getFullYear()} KnowPath</span></div></div>`;
}
function subbar() {
  const title = route().name === 'overview' ? '我的学习空间' : routes.find(r => r[0] === route().name)?.[1] || (route().name === 'assistant' ? '学习助手' : '学习空间');
  return `<div class="subbar"><div class="subbar-title">${icon('layers')}${title}</div><div class="subbar-right"><span class="date-label">${new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())}</span><button class="mode-pill ${state.mode}" data-action="settings" title="${state.mode === 'demo' ? '正在使用内置示例；点击连接真实后端' : '已选择本地后端模式；点击管理连接'}"><span class="mode-dot"></span>${state.mode === 'demo' ? '示例体验' : state.error ? '连接需检查' : '本地空间'}${icon('down')}</button></div></div>`;
}
function pageHeading(title, description, action = '') { return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function spaceSelect(action = 'select-space') { return `<select class="select-control" aria-label="选择学习空间" data-change="${action}">${data().spaces.map(s => `<option value="${esc(s.id)}" ${s.id === currentSpace()?.id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>`; }
function art(theme = 'blue') {
  if (theme === 'sage') return '<div class="code-art" aria-hidden="true"><div class="dots">•••</div><div>def explore(ideas):<br>&nbsp; for idea in ideas:<br>&nbsp;&nbsp; learn(idea)<br>&nbsp; return possibilities</div></div>';
  if (theme === 'lavender') return '<svg class="network-art" viewBox="0 0 210 140" aria-hidden="true"><g stroke="#cfc9df" stroke-width="1.3"><path d="M40 42 100 27 170 47M40 42 105 78 170 47M40 99 105 78 168 110M40 99 104 122 168 110M100 27 170 110M40 42 104 122M40 99 100 27M105 78 168 110"/></g><g fill="#e9e5f2" stroke="#b6adcc" stroke-width="2"><circle cx="40" cy="42" r="12"/><circle cx="40" cy="99" r="12"/><circle cx="100" cy="27" r="12"/><circle cx="105" cy="78" r="15"/><circle cx="104" cy="122" r="10"/><circle cx="170" cy="47" r="12"/><circle cx="168" cy="110" r="12"/></g></svg>';
  return '<div class="matrix-art" aria-hidden="true"><span>[</span><div>1&nbsp; 0&nbsp; 2<br>0&nbsp; 1&nbsp; 3</div><span>]</span></div>';
}
function spaceCard(space, i) {
  const theme = space.theme || ['blue', 'sage', 'lavender'][i % 3];
  const progress = state.mode === 'demo' ? spaceProgress(space) : null;
  const count = materialIds(space).length;
  return `<article class="space-card"><a class="card-art ${theme}" href="#/space/${encodeURIComponent(space.id)}" aria-label="打开${esc(space.name)}"><span class="art-label">${['让理解更进一步', '给好奇心一个开始', '与新的可能相遇'][i % 3]}</span>${art(theme)}</a><div class="card-content"><div class="card-title-row"><h3><a href="#/space/${encodeURIComponent(space.id)}">${esc(space.name)}</a></h3></div><p>${esc(space.goal || '把你的资料，变成自己的知识。')}</p><div class="card-meta"><span>${icon('file')}${count} 份资料</span><span>${icon('graph')}${(space.topic_ids || []).length} 个学习主题</span></div>${progress == null ? '<div class="empty-progress">从资料出发，逐步建立学习记录</div>' : `<div class="progress" role="progressbar" aria-label="${esc(space.name)}示例掌握度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><div class="progress-fill" style="width:${progress}%"></div></div>`}<div class="card-bottom"><span class="subtle">${progress == null ? esc(statusLabels[space.status] || '学习空间') : `已掌握 ${progress}%`}</span>${link('继续探索', `#/space/${encodeURIComponent(space.id)}`)}</div></div></article>`;
}
function taskTitle(task) { return task.title || task.name || task.context?.title || `${taskLabels[task.type || task.kind] || '学习'}：${(task.topic_ids || []).map(id => (state.topics.get(task.space_id) || byId(task.space_id)?.nodes || []).find(n => n.id === id)?.title || '知识主题').join('、')}`; }
function taskRow(task) {
  const done = task.status === 'completed';
  const kind = task.type || task.kind || 'learn';
  return `<div class="task-row"><button class="task-check ${done ? 'done' : ''}" data-action="complete-task" data-id="${esc(task.id)}" aria-label="${done ? '已完成' : '完成任务'}：${esc(taskTitle(task))}" aria-pressed="${done}" ${done && state.mode === 'live' ? 'disabled' : ''}>${done ? icon('check') : ''}</button><div class="task-content"><div class="task-title ${done ? 'completed' : ''}">${esc(taskTitle(task))}</div><div class="task-detail">${esc(byId(task.space_id)?.name || currentSpace()?.name || '')} · ${Number(task.minutes || task.estimated_minutes || 25)} 分钟</div></div><span class="task-tag ${kind === 'review' ? 'review' : ''}">${taskLabels[kind] || '学习'}</span></div>`;
}
function overview() {
  const spaces = data().spaces.filter(s => s.status !== 'archived');
  const tasks = activeTasks();
  const nodes = spaces.flatMap(s => s.nodes || []);
  const stats = [['layers', spaces.length, '个', '学习空间'], ['file', data().materials.length, '份', '学习资料'], ['graph', state.mode === 'demo' ? nodes.length : '—', '个', '连接的知识点'], ['check', tasks.filter(t => t.status === 'completed').length, '项', '已完成任务']];
  return `<section class="hero" aria-labelledby="hero-title"><div class="hero-copy"><div class="hero-overline">${icon('spark')}每一点好奇，都值得继续。</div><h1 id="hero-title">让每一步学习，<br>都有方向。</h1><p class="hero-description">把零散的资料，连成清晰的知识。<br>在属于你的节奏里，让理解自然发生。</p><div class="hero-actions">${button('继续学习', 'continue-learning', 'button button-primary', 'arrow')}${link('探索知识图谱', '#/graph')}</div></div><img class="hero-art" src="./assets/knowledge-orbit.svg" alt="知识围绕学习目标连接，形成清晰的探索路径" width="640" height="500"><div class="hero-caption">每个知识点，都能找到彼此。</div></section><div class="stats-strip" aria-label="${state.mode === 'demo' ? '示例学习统计' : '学习统计'}">${stats.map(([glyph, value, unit, label]) => `<div class="stat"><span class="stat-icon">${icon(glyph)}</span><div><div class="stat-value">${value}<small>${unit}</small></div><div class="stat-label">${label}</div></div></div>`).join('')}</div><section class="section"><div class="section-heading"><div><h2>你的学习，此刻继续。</h2><p>熟悉的知识，新一点的发现。</p></div>${link('全部学习空间', '#/spaces')}</div>${spaces.length ? `<div class="space-grid overview-spaces">${spaces.slice(0, 3).map(spaceCard).join('')}</div>` : empty('给好奇心一个空间', '先导入一份资料，再创建属于你的学习空间。', button('创建学习空间', 'new-space', 'button button-primary', 'plus'))}</section><div class="quick-section"><section class="panel"><div class="panel-title"><h2>今天，向前一小步。</h2>${link('查看计划', '#/plan')}</div>${tasks.length ? tasks.slice(0, 3).map(taskRow).join('') : `<p class="subtle">还没有学习任务。创建计划，让下一步更清晰。</p>${link('安排学习计划', '#/plan')}`}</section><section class="panel tip-panel">${icon('leaf')}<h2>不必一口气学会。<br>只要每一次，都有收获。</h2><p>遇到不明白的地方，就从一个问题开始。<br>让学习助手陪你，把知识一点点想清楚。</p>${link('和学习助手聊聊', '#/assistant')}<div class="tip-decoration"></div></section></div>`;
}
function filteredSpaces() {
  return data().spaces.filter(s => (state.spaceFilter === 'all' || (state.spaceFilter === 'active' ? s.status !== 'archived' : s.status === 'archived')) && `${s.name} ${s.goal || ''}`.toLowerCase().includes(state.query.toLowerCase()));
}
function spacesPage() {
  return `${pageHeading('学习空间', '每一个目标，都值得一个专属空间。', button('新建空间', 'new-space', 'button button-primary', 'plus'))}<div class="toolbar"><div class="filter-group" role="group" aria-label="学习空间筛选">${[['all', '全部空间'], ['active', '学习中'], ['archived', '已归档']].map(([id, label]) => `<button class="filter-button ${state.spaceFilter === id ? 'active' : ''}" data-action="space-filter" data-id="${id}" aria-pressed="${state.spaceFilter === id}">${label}</button>`).join('')}</div><label class="search-field">${icon('search')}<input type="search" data-input="space-search" placeholder="搜索你的学习空间" aria-label="搜索你的学习空间" value="${esc(state.query)}"></label></div><div id="space-results">${spaceResults()}</div>`;
}
function spaceResults() { const spaces = filteredSpaces(); return spaces.length ? `<div class="space-grid">${spaces.map(spaceCard).join('')}</div>` : empty('这里还没有学习空间', state.query ? '换一个关键词试试。' : '导入资料，给自己的学习目标一个开始。', button('创建学习空间', 'new-space', 'button button-primary', 'plus')); }
function normalizeType(material) { const type = String(material.type || material.name.split('.').pop()).toLowerCase(); return type.includes('pdf') ? 'pdf' : type.includes('mark') || type === 'md' ? 'md' : 'txt'; }
function materialResults() {
  const materials = data().materials.filter(m => (state.materialFilter === 'all' || normalizeType(m) === state.materialFilter) && m.name.toLowerCase().includes(state.query.toLowerCase()));
  if (!materials.length) return empty('还没有找到资料', state.query ? '试试其他文件名，或者清空筛选条件。' : '你的教材、笔记和灵感，都可以从这里开始。', button('选择文件', 'upload', 'button button-primary', 'upload'), 'file');
  return `<div class="material-list"><div class="material-head"><span>资料名称</span><span>状态</span><span class="material-size">文件大小</span><span class="material-date">导入日期</span><span class="material-action">操作</span></div>${materials.map(m => `<div class="material-row"><button class="material-name" data-action="view-material" data-id="${esc(m.id)}"><span class="file-icon ${normalizeType(m)}"><span class="file-type">${normalizeType(m).toUpperCase()}</span></span><span title="${esc(m.name)}">${esc(m.name)}</span></button><span class="material-status"><span class="status-tag ${m.status !== 'ready' ? 'pending' : ''}">${statusLabels[m.status] || esc(m.status)}</span></span><span class="material-size">${sizeText(m.size_bytes)}</span><span class="material-date">${dateText(m.created_at)}</span><span class="material-action">${button('查看', 'view-material', 'text-link', '', `data-id="${esc(m.id)}"`)}</span></div>`).join('')}</div>`;
}
function materialsPage() {
  return `${pageHeading('资料库', '让读过的每一页，都成为自己的知识。', button('刷新', 'refresh', 'button button-secondary', 'refresh'))}<div class="upload-zone" id="upload-zone"><div class="upload-symbol">${icon('upload')}</div><div><h2>新的知识，从一份资料开始。</h2><p>拖拽文件到这里，支持 PDF、Markdown、TXT，单个文件不超过 20 MB</p></div><label class="button button-secondary file-picker">${icon('plus')}选择文件<input id="file-input" type="file" accept=".pdf,.md,.txt" aria-label="选择学习资料" multiple></label></div>${state.mode === 'demo' ? notice('示例模式下，文本资料可在本地预览；PDF 仅保存文件信息。连接本地服务后可进行完整解析。') : ''}<div class="toolbar"><div class="filter-group" role="group" aria-label="资料类型筛选">${[['all', '全部资料'], ['pdf', 'PDF'], ['md', 'Markdown'], ['txt', 'TXT']].map(([id, label]) => `<button class="filter-button ${state.materialFilter === id ? 'active' : ''}" data-action="material-filter" data-id="${id}" aria-pressed="${state.materialFilter === id}">${label}</button>`).join('')}</div><label class="search-field">${icon('search')}<input type="search" data-input="material-search" placeholder="搜索资料名称" aria-label="搜索资料名称" value="${esc(state.query)}"></label></div><div id="material-results">${materialResults()}</div>`;
}
function graphData() { return state.mode === 'demo' ? { nodes: currentSpace()?.nodes || [], edges: currentSpace()?.edges || [] } : state.graph || { nodes: [], edges: [] }; }
function graphSvg() {
  const { nodes, edges } = graphData();
  const shown = nodes.slice(0, 100);
  const positions = new Map(shown.map((n, i) => {
    if (!i) return [n.id, [385, 256]];
    const ring = i > 12 ? 2 : 1, count = ring === 1 ? Math.min(shown.length - 1, 12) : shown.length - 13;
    const angle = (i - (ring === 1 ? 1 : 13)) / Math.max(1, count) * Math.PI * 2 - Math.PI / 2;
    return [n.id, [385 + Math.cos(angle) * (ring === 1 ? 210 : 310), 256 + Math.sin(angle) * (ring === 1 ? 155 : 225)]];
  }));
  return `<svg class="graph-svg" viewBox="0 0 770 530" role="group" aria-label="${esc(currentSpace()?.name)}知识关系图，使用 Tab 选择节点"><g id="graph-transform" transform="translate(${385 + state.pan.x} ${265 + state.pan.y}) scale(${state.zoom}) translate(-385 -265)">${edges.filter(e => positions.has(e.from_id) && positions.has(e.to_id)).map(e => { const a = positions.get(e.from_id), b = positions.get(e.to_id); return `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="#cfdce6" stroke-width="1.5"/>`; }).join('')}${shown.map((n, i) => { const [x, y] = positions.get(n.id); const label = n.title || n.name || '知识点'; const selected = (state.graphSelected || shown[0]?.id) === n.id; return `<g class="graph-node ${selected ? 'selected' : ''}" data-action="select-node" data-id="${esc(n.id)}" transform="translate(${x} ${y})" role="button" tabindex="0" aria-label="查看知识点：${esc(label)}" aria-pressed="${selected}"><circle r="${i ? 29 : 43}" fill="${n.status === 'mastered' ? '#dce8df' : n.status === 'learning' ? '#e4dded' : '#dce8f2'}"/><text class="node-glyph" text-anchor="middle" dy="6">${esc(label.slice(0, 1))}</text><text text-anchor="middle" y="${i ? 51 : 66}">${esc(label.length > 13 ? label.slice(0, 12) + '…' : label)}</text><title>${esc(label)}</title></g>`; }).join('')}</g></svg>`;
}
function graphDetail() {
  const { nodes, edges } = graphData();
  const node = nodes.find(n => n.id === state.graphSelected) || nodes[0];
  if (!node) return '';
  const connections = edges.filter(e => e.from_id === node.id || e.to_id === node.id);
  return `<div class="detail-icon">${icon('graph')}</div><h2>${esc(node.title || node.name)}</h2><p>${esc(node.description || '查看与这个主题相关的知识关系，并回到原始资料继续学习。')}</p><div class="detail-label">${state.mode === 'demo' ? '示例学习状态' : '主题状态'}</div><div class="detail-value">${({ mastered: '已掌握', learning: '学习中', new: '待探索', active: '有效主题' })[node.status] || '待探索'}</div><div class="detail-label">知识连接</div><div class="detail-value">${connections.length} 条关联 · ${node.source_refs?.length || 0} 条来源</div>${(node.source_refs || []).slice(0, 5).map((ref, i) => `<button class="citation" data-action="node-source" data-id="${esc(node.id)}" data-index="${i}">${icon('file')}查看来源 ${i + 1}</button>`).join('')}${button('围绕这个主题提问', 'ask-node', 'button button-soft', 'spark', `data-id="${esc(node.id)}"`)}`;
}
function graphPage() {
  const heading = pageHeading('知识图谱', '把知识连起来，让理解看得见。');
  if (!currentSpace()) return heading + empty('先创建一个学习空间', '将资料放入空间，才能探索它们之间的联系。', button('新建空间', 'new-space', 'button button-primary', 'plus'), 'graph');
  const graph = graphData();
  return `${heading}<div class="toolbar graph-toolbar">${spaceSelect()}<div>${button('重新加载', 'reload-graph', 'button button-secondary', 'refresh')}</div></div>${state.graphError ? notice(esc(state.graphError), 'error') : ''}${state.graphLoading ? '<div class="skeleton" role="status" aria-label="正在加载知识图谱"></div>' : graph.nodes.length ? `<div class="graph-layout"><div class="graph-stage"><div class="graph-legend">${state.mode === 'demo' ? '<span class="legend-item"><i class="legend-dot sage"></i>已掌握</span><span class="legend-item"><i class="legend-dot lavender"></i>学习中</span><span class="legend-item"><i class="legend-dot"></i>待探索</span>' : '<span class="legend-item"><i class="legend-dot"></i>来自绑定资料的知识主题</span>'}</div>${graphSvg()}<div class="graph-tools">${button('', 'zoom-out', 'icon-button', 'minus', 'aria-label="缩小图谱"')}${button('', 'zoom-reset', 'icon-button', 'reset', 'aria-label="重置图谱视图"')}${button('', 'zoom-in', 'icon-button', 'plus', 'aria-label="放大图谱"')}</div></div><aside class="panel graph-detail" id="graph-detail" aria-live="polite">${graphDetail()}</aside></div><p class="source-ref">${graph.nodes.length} 个知识点 · ${graph.edges.length} 条关系 · 拖动画布平移，点击节点查看详情${graph.nodes.length > 100 ? ' · 当前展示前 100 个节点' : ''}</p>` : empty('知识正在等待连接', '当前资料尚未包含可展示的知识点。请先检查资料解析与图谱发布状态。', link('前往资料库', '#/materials', 'button button-soft'), 'graph')}`;
}
function weekStrip() {
  const today = new Date(), monday = new Date(today); monday.setDate(today.getDate() - (today.getDay() + 6) % 7);
  return `<div class="week-strip" aria-label="本周日期">${['一', '二', '三', '四', '五', '六', '日'].map((label, i) => { const date = new Date(monday); date.setDate(monday.getDate() + i); const current = date.toDateString() === today.toDateString(); return `<div class="day ${current ? 'today' : ''}" ${current ? 'aria-current="date"' : ''}>周${label}<strong>${date.getDate()}</strong></div>`; }).join('')}</div>`;
}
function timerText() { const remaining = state.timerEnd ? Math.max(0, Math.ceil((state.timerEnd - Date.now()) / 1000)) : state.timerRemaining; return `${Math.floor(remaining / 60).toString().padStart(2, '0')}:${(remaining % 60).toString().padStart(2, '0')}`; }
function focusCard() { return `<aside class="panel focus-card">${icon('leaf')}<h2>专注这一刻</h2><p>给自己 ${state.timerDuration} 分钟，不急不躁。</p><div class="timer" id="timer" role="timer" aria-label="专注倒计时">${timerText()}</div><div class="timer-actions">${button(state.timerEnd ? '暂停专注' : state.timerRemaining === 0 ? '再来一次' : '开始专注', 'toggle-timer', 'button button-primary', state.timerEnd ? 'pause' : 'play')}${button('', 'reset-timer', 'icon-button', 'refresh', 'aria-label="重置专注计时器"')}</div><div class="timer-note">计时器仅帮助安排时间，<br>完成学习任务需要你亲自确认。</div></aside>`; }
function planPage() {
  const space = currentSpace(), tasks = activeTasks().filter(t => !space || t.space_id === space.id);
  const heading = pageHeading('学习计划', '按自己的节奏，积累看得见的进步。', button('安排计划', 'new-plan', 'button button-primary', 'plus'));
  if (!space) return heading + empty('学习，从一个小目标开始', '先创建学习空间，再为它安排学习计划。', button('新建空间', 'new-space', 'button button-primary', 'plus'), 'calendar');
  return `${heading}<div class="toolbar">${spaceSelect()}<span class="subtle">${tasks.filter(t => t.status === 'completed').length} / ${tasks.length} 项已完成</span></div>${state.planError ? notice(esc(state.planError), 'error') : ''}<div class="plan-layout"><div>${weekStrip()}${state.planLoading ? '<div class="skeleton" role="status" aria-label="正在加载学习计划"></div>' : tasks.length ? `<section class="panel plan-list"><div class="panel-title"><h2>留一点时间，给新的理解。</h2><span class="badge">${state.mode === 'demo' ? '示例计划' : '当前计划'}</span></div>${tasks.map(taskRow).join('')}</section>` : empty('下一步，由你开始', '选择每次学习时长，让资料中的知识点成为可执行的计划。', button('安排学习计划', 'new-plan', 'button button-primary', 'calendar'), 'calendar')}</div>${focusCard()}</div>`;
}
function chatMessages() {
  return (state.messages.get(currentSpace()?.id) || []).map((message, index) => `<div class="chat-message ${message.role}"><span class="chat-message-label">${message.role === 'user' ? '你' : state.mode === 'demo' ? '学习助手 · 本地示例资料摘录' : 'KnowPath 学习助手'}</span>${esc(message.text)}${(message.citations || []).map((ref, i) => `<button class="citation" data-action="chat-source" data-message="${index}" data-index="${i}">${esc(ref.material_name || data().materials.find(m => m.id === ref.material_id)?.name || '来源资料')} · 查看依据 ${i + 1}</button>`).join('')}</div>`).join('');
}
function assistantPage() {
  const heading = pageHeading('学习助手', '带着问题出发，带着理解回来。');
  if (!currentSpace()) return heading + empty('为提问选择一份知识背景', '创建学习空间后，助手才能在你的资料范围内查找依据。', button('新建空间', 'new-space', 'button button-primary', 'plus'), 'spark');
  const pending = state.pendingJob?.spaceId === currentSpace().id;
  return `${heading}<div class="assistant-shell"><div class="toolbar">${spaceSelect()}<span class="badge">${state.mode === 'demo' ? '本地摘录演示' : '基于你的资料'}</span></div>${state.mode === 'demo' ? notice('当前展示本地资料摘录，不调用 AI 模型。连接服务后可使用带来源引用的真实问答。') : ''}<div class="assistant-intro"><div class="assistant-mark">${icon('spark')}</div><h2>今天，想弄懂什么？</h2><p>不必一次问得完美。从一个小小的好奇开始。</p><div class="suggestions">${['帮我梳理这份资料的重点', '如何理解这个知识点？', '接下来可以学习什么？'].map(text => `<button class="suggestion" data-action="suggestion" data-text="${esc(text)}">${text}</button>`).join('')}</div></div><div class="chat-messages" id="chat-messages" aria-live="polite" aria-relevant="additions">${chatMessages()}</div><div id="chat-status" class="notice" role="status" ${!state.sending && !pending ? 'hidden' : ''}>${state.sending ? '正在检索资料与生成回答…' : '上次回答尚未接收完成，可继续接收同一个任务。'}</div><form id="chat-form" class="chat-form"><label class="screenreader" for="question">输入你的学习问题</label><textarea id="question" name="question" rows="2" maxlength="8000" placeholder="例如：能用直观的方式解释一下线性变换吗？" required ${state.sending ? 'disabled' : ''}></textarea><div class="chat-form-bottom"><span>回答有迹可循，来源随时可查<br>Ctrl / ⌘ + Enter 发送</span>${state.sending ? button('停止', 'stop-chat', 'button button-secondary', 'pause') : pending ? button('继续接收', 'resume-chat', 'button button-primary', 'refresh') : '<button type="submit" class="button button-primary">发送问题' + icon('arrow') + '</button>'}</div></form><p class="chat-footnote">${state.mode === 'demo' ? '示例仅用于体验交互，不代表 AI 生成效果。' : '模型回答可能存在偏差，请结合原始资料核对。'}</p></div>`;
}
function spacePage(spaceId) {
  const space = byId(spaceId);
  if (!space) return pageHeading('学习空间', '每一个目标，都值得一个专属空间。') + empty('没有找到这个空间', '它可能已被移除，或当前数据模式已发生变化。', link('返回学习空间', '#/spaces', 'button button-soft'));
  state.selected = space.id;
  const materials = data().materials.filter(m => materialIds(space).includes(m.id));
  const topics = state.mode === 'demo' ? space.nodes : state.topics.get(space.id);
  return `<a class="text-link back-link" href="#/spaces">返回学习空间</a>${pageHeading(esc(space.name), '在这里，把一份资料变成一次理解。', button('选择学习范围', 'edit-scope', 'button button-secondary', 'settings'))}<div class="details-grid"><div><section class="detail-hero"><span class="badge">${state.mode === 'demo' ? '示例空间' : '个人学习空间'}</span><h2 style="margin-top:20px">${esc(space.name)}</h2><p>${esc(space.goal || '还没有设置具体目标。从一个感兴趣的主题开始也很好。')}</p>${link('进入学习计划', '#/plan', 'button button-primary', 'arrow')}</section><section class="panel" style="margin-top:24px"><div class="panel-title"><h2>知识主题</h2>${link('查看图谱', '#/graph')}</div>${topics ? `<div class="topic-list">${topics.map(t => `<button class="topic-chip" data-action="explore-topic" data-id="${esc(t.id)}">${esc(t.title || t.name)}</button>`).join('') || '<p class="subtle">暂无主题，请检查资料的解析与发布状态。</p>'}</div>` : '<p class="subtle">正在读取资料中的主题…</p>'}</section></div><aside class="panel" style="align-self:start"><div class="panel-title"><h2>空间里的资料</h2><span class="badge">${materials.length} 份</span></div>${materials.map(m => `<div class="list-item">${icon('file')}<span>${esc(m.name)}</span>${button('查看', 'view-material', 'text-link', '', `data-id="${esc(m.id)}"`)}</div>`).join('')}<div class="mini-heading">每周学习预算</div><p class="subtle">${space.weekly_minutes || '未设置'} ${space.weekly_minutes ? '分钟' : ''}</p><div class="mini-heading">已经选择的学习主题</div><p class="subtle">${space.topic_ids?.length || 0} 个</p><div class="large-callout"><p>有不理解的地方？让问题带你走得更远。</p>${link('问问学习助手', '#/assistant')}</div></aside></div>`;
}
function render() {
  renderVersion++; header();
  const current = route();
  const views = { overview, spaces: spacesPage, materials: materialsPage, graph: graphPage, plan: planPage, assistant: assistantPage, space: () => spacePage(current.id) };
  let content;
  if (state.loading) content = pageHeading('正在连接你的知识', '读取学习空间和资料，请稍候。') + '<div class="skeleton" role="status" aria-label="正在加载数据"></div>';
  else if (state.error && state.mode === 'live') content = pageHeading('让连接，重新发生。', '检查本地服务，然后继续你的学习。') + notice(esc(state.error), 'error') + empty('暂时无法读取学习数据', '请检查后端启动情况与本地会话令牌。你的真实数据仍保留在后端。', button('连接设置', 'settings', 'button button-primary', 'connection') + ' ' + button('重试', 'refresh', 'button button-secondary', 'refresh'), 'connection');
  else content = (views[current.name] || (() => empty('这个页面还不存在', '回到概览，继续你的探索。', link('返回概览', '#/overview', 'button button-primary'))))();
  main.innerHTML = `<div class="container">${subbar()}${content}</div>`;
  document.title = `${current.name === 'overview' ? '让每一步学习，都有方向' : routes.find(r => r[0] === current.name)?.[1] || (current.name === 'assistant' ? '学习助手' : '学习空间')} · KnowPath`;
  bindGraphDrag();
  if (state.selected) storageWrite(session, `knowpath-selected-${state.mode}`, state.selected);
}
async function refreshData() {
  if (state.mode === 'demo') { render(); return; }
  state.loading = true; state.error = ''; render();
  try {
    const [spaces, materials] = await Promise.all([api.spaces(), api.materials()]);
    if (state.mode !== 'live') return;
    state.live = { spaces, materials, tasks: [] };
    if (!spaces.some(s => s.id === state.selected)) state.selected = spaces[0]?.id || '';
  } catch (error) { if (state.mode === 'live') state.error = error.message; }
  finally { state.loading = false; render(); }
  await loadRoute();
}
async function loadRoute() {
  if (state.mode !== 'live' || state.error || state.loading) return;
  const current = route(), space = currentSpace();
  if (!space) return;
  if (current.name === 'graph' && state.graphSpace !== space.id && !state.graphLoading) {
    state.graphLoading = true; state.graphError = ''; state.graph = null; render();
    try { const result = await api.graph(space); if (state.mode === 'live' && currentSpace()?.id === space.id) { state.graph = result; state.graphSpace = space.id; state.topics.set(space.id, result.nodes); } }
    catch (error) { if (currentSpace()?.id === space.id) state.graphError = error.message; }
    finally { state.graphLoading = false; if (route().name === 'graph') { render(); if (currentSpace()?.id !== space.id) void loadRoute(); } }
  }
  if (current.name === 'space' && !state.topics.has(space.id)) {
    try { const topics = await api.topics(space); if (state.mode === 'live') { state.topics.set(space.id, topics); if (route().name === 'space') render(); } }
    catch (error) { toast(error.message, true); }
  }
  if (current.name === 'plan' && !state.plans.has(space.id)) {
    const planId = storageRead(session, 'knowpath-plan-ids', {})[space.id];
    if (planId) {
      state.planLoading = true; state.planError = ''; render();
      try { const plan = await api.plan(planId); if (state.mode === 'live') state.plans.set(space.id, { ...plan, space_id: space.id }); }
      catch (error) { state.planError = error.message; }
      finally { state.planLoading = false; if (route().name === 'plan') render(); }
    }
  }
}

function settingsModal() {
  openModal('连接你的学习空间', `<p class="modal-intro">示例体验随时可用。连接本地服务后，即可访问你自己的资料与学习记录。</p><form id="connection-form"><div class="form-field"><label for="service-address">本地服务地址</label><input id="service-address" value="http://127.0.0.1:8000" readonly><small>与项目中的 Python 后端保持一致。</small></div><div class="form-field"><label for="local-token">本地会话令牌</label><input id="local-token" name="token" type="password" autocomplete="off" spellcheck="false" placeholder="输入本地服务令牌" value="${esc(token)}"><small>只保存在当前标签页，不会写入前端代码。</small></div><div class="modal-note">在项目的 py 目录启动后端服务，然后填入本地令牌。详细命令见 frontend/README.md。</div><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('使用示例体验', 'demo-mode', 'button button-secondary')}<button class="button button-primary" type="submit">${icon('connection')}连接本地服务</button></div></form><div class="connection-state" role="status"></div>`);
}
function newSpaceModal() {
  const choices = data().materials.filter(m => m.status === 'ready');
  openModal('给学习，一个新空间', `<p class="modal-intro">从一个你想弄懂的主题开始。选择相关资料，KnowPath 会帮你把知识组织起来。</p>${!choices.length ? notice('需要至少一份已就绪的资料。真实模式下，资料还需完成图谱审核发布。', 'warning') : ''}<form id="new-space-form"><div class="form-field"><label for="space-name">空间名称</label><input id="space-name" name="name" maxlength="100" placeholder="例如：从零理解线性代数" required></div><div class="form-field"><label for="space-goal">你想达到什么目标？</label><textarea id="space-goal" name="goal" maxlength="1000" rows="2" placeholder="例如：能够直观理解矩阵与线性变换"></textarea></div><fieldset class="form-field"><legend>选择学习资料（1—5 份）</legend><div class="material-choices">${choices.map(m => `<label class="check-label"><input type="checkbox" name="material_ids" value="${esc(m.id)}">${esc(m.name)}</label>`).join('') || '<p class="subtle">先在资料库导入第一份资料。</p>'}</div></fieldset><div class="form-field"><label for="weekly-minutes">每周准备投入多少时间？</label><select id="weekly-minutes" name="weekly_minutes"><option value="60">60 分钟，轻松开始</option><option value="150" selected>150 分钟，稳步学习</option><option value="300">300 分钟，深入探索</option></select></div><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button class="button button-primary" type="submit" ${!choices.length ? 'disabled' : ''}>创建学习空间</button></div></form>${!choices.length ? link('前往资料库', '#/materials') : ''}`);
}
async function createSpace(form) {
  const values = new FormData(form);
  const ids = values.getAll('material_ids');
  const name = values.get('name').trim(), goal = values.get('goal').trim();
  if (!name) throw new Error('请给学习空间起一个名字。');
  if (ids.length < 1 || ids.length > 5) throw new Error('请选择 1—5 份学习资料。');
  const payload = { name, material_ids: ids, weekly_minutes: Number(values.get('weekly_minutes')), ...(goal ? { goal } : {}) };
  let space;
  if (state.mode === 'demo') {
    const spaceId = `demo-${uid()}`, nodes = [];
    for (const materialId of ids) {
      const material = demo.materials.find(m => m.id === materialId);
      const headings = [...(material.text || '').matchAll(/^#{1,3}\s+(.+)$/gm)].map(m => m[1]);
      for (const title of headings.length ? headings : [material.name.replace(/\.(md|txt)$/i, '')]) nodes.push({ id: `${spaceId}-t${nodes.length}`, title, status: 'new', description: '从本地资料的标题提取的示例主题。', source_refs: [{ material_id: materialId }] });
    }
    space = { ...payload, id: spaceId, status: 'active', theme: ['blue', 'sage', 'lavender'][demo.spaces.length % 3], nodes, edges: nodes.slice(1).map((node, i) => ({ id: `${spaceId}-e${i}`, from_id: nodes[0].id, to_id: node.id, type: 'contains' })), topic_ids: nodes.map(n => n.id), created_at: new Date().toISOString(), space_version: 1 };
    demo.spaces.push(space); saveDemo();
  } else { space = await api.createSpace(payload); state.live.spaces.push(space); }
  state.selected = space.id; closeModal(); go('space', space.id); toast('学习空间已创建，从一个知识点开始吧。');
}
async function uploadFiles(files) {
  if (!files?.length) return;
  const selected = [...files];
  for (const file of selected) validateFile(file);
  if (selected.length > 10) throw new Error('一次最多上传 10 个文件，请分批选择。');
  openModal('导入学习资料', `<p class="modal-intro" id="upload-progress" role="status">准备导入 ${selected.length} 份资料…</p><div class="skeleton" style="height:70px"></div>`);
  const errors = []; let completed = 0;
  const mode = state.mode;
  for (const file of selected) {
    try {
      if (mode === 'demo') {
        const isPdf = /\.pdf$/i.test(file.name);
        const text = isPdf ? '' : await file.text();
        if (text.length > 1000000) throw new Error('示例模式仅保存 1 MB 以内的文本内容，大文件请连接后端导入。');
        const candidate = { id: `demo-material-${uid()}`, name: file.name, type: file.name.split('.').pop().toLowerCase(), size_bytes: file.size, text, status: isPdf ? 'uploaded' : 'ready', created_at: new Date().toISOString(), version: 1 };
        const totalText = demo.materials.reduce((sum, m) => sum + (m.text?.length || 0), 0);
        if (totalText + text.length > 1800000) throw new Error('示例资料容量已满，请连接后端管理更多资料。');
        demo.materials.unshift(candidate); saveDemo();
      } else { const uploaded = await api.upload(file); state.live.materials.unshift(uploaded.material); }
      completed++;
    } catch (error) { errors.push(`${file.name}：${error.message}`); }
    const progress = $('#upload-progress'); if (progress) progress.textContent = `已完成 ${completed} / ${selected.length} 份资料`;
  }
  if (errors.length) openModal('资料导入结果', `<p class="modal-intro">成功导入 ${completed} 份资料。以下文件需要处理后重试：</p>${errors.map(error => notice(esc(error), 'error')).join('')}<div class="form-actions">${button('知道了', 'close-modal', 'button button-primary')}</div>`);
  else { closeModal(); toast(mode === 'demo' ? '资料已保存到此浏览器。' : '资料已上传，后台正在解析。稍后刷新查看状态。'); }
  state.materialFilter = 'all'; state.query = ''; go('materials');
}
async function viewMaterial(materialId) {
  const material = data().materials.find(m => m.id === materialId);
  if (!material) throw new Error('资料不存在，请刷新后重试。');
  if (state.mode === 'demo') {
    openModal(esc(material.name), `<p class="modal-intro">${normalizeType(material).toUpperCase()} · ${sizeText(material.size_bytes)} · 本地示例资料</p>${material.text ? `<div class="source-content">${esc(material.text)}</div>` : notice('PDF 文件信息已保存。连接本地服务并重新导入此文件，才能解析与查看正文。', 'warning')}<div class="form-actions">${button('关闭', 'close-modal', 'button button-secondary')}</div>`); return;
  }
  openModal(esc(material.name), '<div class="loading-block" role="status">正在读取原始资料…</div>');
  const version = modalVersion;
  const result = await api.material(materialId);
  if (version !== modalVersion || !modal.open) return;
  const chunks = result.version?.chunks || [];
  openModal(esc(material.name), `<p class="modal-intro">${statusLabels[result.material.status] || esc(result.material.status)} · ${chunks.length} 个资料片段</p>${chunks.length ? `<div class="source-content">${chunks.map(c => `${c.section_path?.length ? esc(c.section_path.join(' / ')) + '\n' : ''}${esc(c.text)}`).join('\n\n')}</div>` : notice('资料尚未完成解析。请确认图谱后台进程已运行，稍后刷新。')}<div class="form-actions">${button('审核知识图谱', 'review-material', 'button button-soft', 'graph', `data-id="${esc(materialId)}"`)}${button('关闭', 'close-modal', 'button button-secondary')}</div>`);
}
async function reviewMaterial(materialId) {
  openModal('审核知识图谱', '<div class="loading-block" role="status">正在读取候选知识结构…</div>');
  const version = modalVersion, diff = await api.graphDiff(materialId);
  if (version !== modalVersion || !modal.open) return;
  const changes = [...(diff.added || []).map(t => ({ label: '新增', item: t })), ...(diff.changed || []).map(t => ({ label: '修改', item: t })), ...(diff.removed || []).map(t => ({ label: '移除', item: t }))];
  state.review = { materialId, diff };
  const hasConflicts = Boolean(diff.conflicts?.length);
  openModal('审核知识图谱', `<p class="modal-intro">确认资料中的主题变化后，发布一个新的知识快照。已有学习空间不会自动切换资料版本。</p>${!diff.candidate_revision_id ? notice(diff.base_graph_version ? '当前图谱已发布，没有待审核的更新。可以用这份资料创建学习空间。' : '尚未生成候选图谱，请等待解析完成后刷新。') : `<div class="source-content">${changes.map(({ label, item }) => `${label}：${esc(item.title || item.after?.title || item.name || item.topic_id || item.id || '主题变更')}`).join('\n') || '没有主题差异。'}</div>${hasConflicts ? notice(`存在 ${diff.conflicts.length} 项知识冲突，需要通过后端审核工具解决后再发布。此页面不会自动替你选择冲突结论。`, 'warning') : ''}${diff.status !== 'pending_review' ? notice('图谱索引仍在准备中，请稍后重新打开审核。', 'warning') : ''}`}<div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('关闭', 'close-modal', 'button button-secondary')}${diff.candidate_revision_id ? button('确认并发布', 'publish-graph', 'button button-primary', 'check', hasConflicts || diff.status !== 'pending_review' ? 'disabled' : '') : ''}</div>`);
}
async function scopeModal() {
  const space = currentSpace(); if (!space) return newSpaceModal();
  openModal('选择这次的学习范围', '<div class="loading-block" role="status">正在读取知识主题…</div>');
  const version = modalVersion;
  const topics = state.mode === 'demo' ? space.nodes : await api.topics(space);
  if (version !== modalVersion || !modal.open) return;
  state.topics.set(space.id, topics);
  openModal('选择这次的学习范围', `<p class="modal-intro">先专注于真正想弄懂的内容。必要的前置知识会一并纳入。调整范围后，现有计划可能需要重新安排。</p><form id="scope-form" data-space="${esc(space.id)}"><div class="material-choices" style="max-height:330px">${topics.map(t => `<label class="check-label"><input type="checkbox" name="topic_ids" value="${esc(t.id)}" ${space.topic_ids?.includes(t.id) ? 'checked' : ''}>${esc(t.title || t.name)}</label>`).join('') || notice('没有可选择的主题，请先检查资料的解析与发布状态。')}</div><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button type="submit" class="button button-primary" ${topics.length ? '' : 'disabled'}>保存学习范围</button></div></form>`);
}
function planModal() {
  const space = currentSpace(); if (!space) return newSpaceModal();
  if (!space.topic_ids?.length) { toast('先选择学习主题，再安排计划。'); return scopeModal(); }
  openModal('找到适合你的节奏', `<p class="modal-intro">为「${esc(space.name)}」安排一段轻松、可执行的学习。${state.mode === 'demo' ? '示例模式使用本地主题生成演示任务。' : '后端会结合前置关系和时间预算安排任务。'}</p><form id="plan-form" data-space="${esc(space.id)}"><div class="form-field"><label for="session-count">安排几次学习？</label><select id="session-count" name="session_count"><option value="3">3 次</option><option value="4">4 次</option><option value="5">5 次</option></select></div><div class="form-field"><label for="session-minutes">每次学习多久？</label><select id="session-minutes" name="minutes_per_session"><option value="15">15 分钟，轻松起步</option><option value="25" selected>25 分钟，保持专注</option><option value="45">45 分钟，深入理解</option></select></div><label class="check-label"><input type="checkbox" name="include_review" checked>在计划中安排温习，让知识更牢固</label><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button type="submit" class="button button-primary">生成学习计划</button></div></form>`);
}
async function createPlan(form) {
  const space = byId(form.dataset.space), values = new FormData(form);
  const payload = { session_count: Number(values.get('session_count')), minutes_per_session: Number(values.get('minutes_per_session')), include_review: values.has('include_review'), rebuild_mode: 'initial' };
  if (space.weekly_minutes && payload.session_count * payload.minutes_per_session > space.weekly_minutes) throw new Error(`当前每周预算为 ${space.weekly_minutes} 分钟，请减少次数或缩短每次时长。`);
  if (state.mode === 'demo') {
    const selected = space.nodes.filter(n => space.topic_ids.includes(n.id));
    if (!selected.length) throw new Error('请先选择至少一个学习主题。');
    demo.tasks = demo.tasks.filter(t => t.space_id !== space.id || t.status === 'completed');
    for (let i = 0; i < payload.session_count; i++) { const topic = selected[i % selected.length]; const type = payload.include_review && i === payload.session_count - 1 ? 'review' : 'learn'; demo.tasks.push({ id: `demo-task-${uid()}`, space_id: space.id, title: `${type === 'review' ? '温习' : '理解'}${topic.title}`, type, minutes: payload.minutes_per_session, status: 'pending', topic_ids: [topic.id] }); }
    saveDemo();
  } else {
    const base = state.plans.get(space.id);
    if (base) Object.assign(payload, { rebuild_mode: 'local_replan', base_plan_id: base.id || base.plan_id, expected_plan_version: base.version });
    const result = await api.createPlan(space.id, payload);
    const plans = storageRead(session, 'knowpath-plan-ids', {}); plans[space.id] = result.plan_id; storageWrite(session, 'knowpath-plan-ids', plans);
    const plan = await api.plan(result.plan_id); state.plans.set(space.id, { ...plan, space_id: space.id });
  }
  state.timerDuration = payload.minutes_per_session; resetTimer(); closeModal(); go('plan'); toast('学习计划已就绪，慢慢来，也会走得很远。');
}
async function completeTask(id) {
  if (state.mode === 'demo') {
    const task = demo.tasks.find(t => t.id === id); if (!task) return;
    task.status = task.status === 'completed' ? 'pending' : 'completed'; saveDemo();
    toast(task.status === 'completed' ? '又前进了一小步，做得不错。' : '任务已恢复为待完成。');
  } else {
    const entry = [...state.plans.entries()].find(([, plan]) => plan.tasks?.some(t => t.id === id));
    if (!entry) throw new Error('没有找到任务，请刷新计划。');
    const [spaceId, plan] = entry, task = plan.tasks.find(t => t.id === id);
    const updated = await api.completeTask(plan, task);
    Object.assign(task, updated); plan.version = updated.plan_version; state.plans.set(spaceId, plan); toast('学习任务已完成。掌握状态仍以测评证据为准。');
  }
  render();
}
function resetTimer() { clearInterval(state.timerInterval); state.timerInterval = null; state.timerEnd = null; state.timerRemaining = state.timerDuration * 60; }
function toggleTimer() {
  if (state.timerEnd) { state.timerRemaining = Math.max(0, Math.ceil((state.timerEnd - Date.now()) / 1000)); state.timerEnd = null; clearInterval(state.timerInterval); }
  else {
    if (!state.timerRemaining) state.timerRemaining = state.timerDuration * 60;
    state.timerEnd = Date.now() + state.timerRemaining * 1000;
    state.timerInterval = setInterval(() => {
      const target = $('#timer'); if (target) target.textContent = timerText();
      if (Date.now() >= state.timerEnd) { clearInterval(state.timerInterval); state.timerEnd = null; state.timerRemaining = 0; toast('这一段专注完成了。休息一下，再继续。'); if (route().name === 'plan') render(); }
    }, 500);
  }
  render();
}
function helpModal() {
  openModal('从好奇，到理解', `<div class="help-content"><h3>1. 先把资料放进来</h3><p>在资料库导入 PDF、Markdown 或 TXT。真实模式下，后台解析完成后，在资料详情中审核并发布图谱快照。</p><h3>2. 为目标创建一个空间</h3><p>选择 1—5 份资料，填写目标与每周时间预算。进入空间后，选定你希望学习的主题。</p><h3>3. 看见知识之间的联系</h3><p>在知识图谱中选择节点查看关系和原文来源。拖动画布调整位置，使用缩放按钮观察细节。</p><h3>4. 按自己的节奏继续</h3><p>安排学习计划，使用专注计时器。完成任务时手动勾选；任务完成并不等于知识已经掌握。</p><h3>5. 带着问题继续探索</h3><p>学习助手基于空间内的资料回答，并提供引用。示例体验只摘录本地示例文本，真实 AI 问答需要本地后端、模型配置和后台进程。</p><h3>用键盘也很顺手</h3><p><code>Ctrl / ⌘ + K</code> 搜索；<code>Esc</code> 关闭弹窗；<code>Ctrl / ⌘ + Enter</code> 发送问题。Tab 可在导航、按钮和图谱节点之间移动。</p></div><div class="form-actions">${button('开始探索', 'close-modal', 'button button-primary')}</div>`);
}
function searchModal() {
  openModal('找到你的下一步', `<div class="command-input">${icon('search')}<input data-input="global-search" id="global-search" aria-label="搜索全部学习内容" placeholder="搜索空间、资料，或前往一个页面" autocomplete="off"><kbd>Esc</kbd></div><div id="global-results">${searchResults('')}</div>`, 'search-dialog');
}
function searchResults(query) {
  const text = query.trim().toLowerCase();
  const groups = [
    ['前往', [...routes, ['assistant', '学习助手']].filter(([, label]) => label.includes(text)).map(([name, label]) => `<a class="search-result" href="#/${name}">${icon(name === 'graph' ? 'graph' : name === 'plan' ? 'calendar' : 'arrow')}<span>${label}</span><small>页面</small></a>`)],
    ['学习空间', data().spaces.filter(s => s.name.toLowerCase().includes(text)).slice(0, 7).map(s => `<a class="search-result" href="#/space/${encodeURIComponent(s.id)}">${icon('layers')}<span>${esc(s.name)}</span><small>学习空间</small></a>`)],
    ['学习资料', data().materials.filter(m => m.name.toLowerCase().includes(text)).slice(0, 7).map(m => `<button class="search-result" data-action="view-material" data-id="${esc(m.id)}">${icon('file')}<span>${esc(m.name)}</span><small>${normalizeType(m).toUpperCase()}</small></button>`)],
  ];
  return groups.some(([, results]) => results.length) ? groups.filter(([, results]) => results.length).map(([title, results]) => `<div class="search-group-title">${title}</div>${results.join('')}`).join('') : '<p class="modal-intro" style="padding-top:24px">没有找到相关内容。试试更短的关键词，例如“矩阵”。</p>';
}
function updateChat() { if (route().name !== 'assistant') return; const question = $('#question')?.value || ''; render(); if ($('#question')) $('#question').value = question; }
async function sendChat(question, resume = false) {
  const space = currentSpace(); if (!space || state.sending) return;
  if (!resume && state.pendingJob) throw new Error('还有一个问题正在处理中。请先继续接收或停止该任务。');
  if (!resume && !question.trim()) throw new Error('先写下一个你想弄懂的问题吧。');
  const list = state.messages.get(space.id) || [];
  if (!resume) { list.push({ role: 'user', text: question }); state.messages.set(space.id, list); }
  state.sending = true; const controller = new AbortController(); state.chatController = controller;
  if ($('#question')) $('#question').value = ''; updateChat();
  try {
    let result;
    if (state.mode === 'demo') {
      const materials = demo.materials.filter(m => materialIds(space).includes(m.id) && m.text);
      if (!materials.length) throw new Error('当前空间还没有可阅读的文本资料，请先导入一份 Markdown 或 TXT。');
      const parts = materials.flatMap(material => material.text.split(/\n\n+/).filter(text => text.length > 20).map(text => ({ text, material })));
      const words = question.toLowerCase().match(/[a-z]+|[\u4e00-\u9fff]{2}/g) || [];
      parts.sort((a, b) => words.filter(w => b.text.toLowerCase().includes(w)).length - words.filter(w => a.text.toLowerCase().includes(w)).length);
      const excerpt = parts.slice(0, 2), cited = [...new Map(excerpt.map(p => [p.material.id, p.material])).values()];
      result = { text: `下面是当前空间资料中的相关摘录：\n\n${excerpt.map(p => p.text).join('\n\n')}\n\n你可以打开来源，结合上下文继续阅读。当前为本地摘录演示，连接服务后可获得针对问题的 AI 讲解。`, citations: cited.map(m => ({ material_id: m.id, material_name: m.name })) };
    } else {
      if (!resume) {
        const job = await api.sendMessage(space.id, question, state.chats.get(space.id));
        state.pendingJob = { ...job, spaceId: space.id }; state.chats.set(space.id, job.session_id);
        if (controller.signal.aborted) { await api.cancelRun(job.run_id); state.pendingJob = null; throw new DOMException('操作已停止', 'AbortError'); }
      }
      result = await api.readMessage(state.pendingJob, { signal: controller.signal, onStatus: text => { const element = $('#chat-status'); if (element) { element.hidden = false; element.textContent = text; } } });
    }
    list.push({ role: 'assistant', text: result.text, citations: result.citations || [] });
    state.messages.set(space.id, list); state.pendingJob = null;
  } catch (error) {
    if (error.name !== 'AbortError') { toast(error.message, true); if (state.pendingJob && error.code !== 'RUN_PENDING' && error.status >= 400) state.pendingJob = null; }
  } finally { state.sending = false; state.chatController = null; updateChat(); }
}
async function showSource(ref) {
  if (state.mode === 'demo' || !ref.material_version_id || !ref.chunk_id) return viewMaterial(ref.material_id);
  openModal('回到知识的来源', '<div class="loading-block" role="status">正在读取来源片段…</div>');
  const version = modalVersion, source = await api.source(ref);
  if (version !== modalVersion || !modal.open) return;
  openModal('回到知识的来源', `<p class="modal-intro">${esc(data().materials.find(m => m.id === ref.material_id)?.name || '原始资料')}${source.page ? ` · 第 ${Number(source.page)} 页` : ''}</p><div class="source-content">${esc(source.text)}</div><p class="source-ref">${esc((source.section_path || []).join(' / '))}</p>`);
}
function graphTransform() { const group = $('#graph-transform'); if (group) group.setAttribute('transform', `translate(${385 + state.pan.x} ${265 + state.pan.y}) scale(${state.zoom}) translate(-385 -265)`); }
function bindGraphDrag() {
  const svg = $('.graph-svg'); if (!svg) return;
  let drag;
  svg.addEventListener('pointerdown', event => { if (event.target.closest('[data-action]')) return; drag = { x: event.clientX, y: event.clientY, ...state.pan }; drag.startX = event.clientX; drag.startY = event.clientY; svg.setPointerCapture(event.pointerId); });
  svg.addEventListener('pointermove', event => { if (!drag) return; const bounds = svg.getBoundingClientRect(); const scale = Math.max(770 / bounds.width, 530 / bounds.height); state.pan = { x: drag.x + (event.clientX - drag.startX) * scale, y: drag.y + (event.clientY - drag.startY) * scale }; graphTransform(); });
  svg.addEventListener('pointerup', () => { drag = null; }); svg.addEventListener('pointercancel', () => { drag = null; });
}

document.addEventListener('click', async event => {
  if (event.target.closest('.skip-link')) { event.preventDefault(); main.focus(); return; }
  const element = event.target.closest('[data-action]');
  if (!element) { if (event.target.closest('a[href^="#/"]') && modal.open) closeModal(); return; }
  const action = element.dataset.action, id = element.dataset.id;
  const actions = {
    'close-modal': closeModal, search: searchModal, help: helpModal,
    settings: () => { settingsModal(); if (state.mode === 'demo') $('.modal-body', modal).insertAdjacentHTML('beforeend', '<div style="margin-top:23px">' + button('恢复初始示例', 'reset-demo', 'text-link') + '</div>'); },
    'reset-demo': () => openModal('恢复初始示例？', '<p class="modal-intro">将移除你在此浏览器中新增的示例空间、示例资料和任务修改，并恢复内置示例。真实后端数据不会改变。</p><div class="form-actions">' + button('取消', 'close-modal', 'button button-secondary') + button('确认恢复示例', 'confirm-reset-demo', 'button button-primary') + '</div>'),
    'confirm-reset-demo': () => { Object.assign(demo, makeDemo()); state.selected = demo.spaces[0].id; state.graphSelected = ''; state.query = ''; state.materialFilter = 'all'; state.spaceFilter = 'all'; state.messages.clear(); saveDemo(); closeModal(); go('overview'); toast('已恢复初始示例。'); },
    privacy: () => openModal('你的知识，安心放在这里', '<div class="help-content"><h3>示例体验</h3><p>示例空间、导入的文本内容和任务状态保存在当前浏览器的 localStorage。不会上传到任何服务。示例 PDF 只保存文件名、大小等信息，不保存文件正文。</p><h3>本地服务</h3><p>真实资料与问答通过本地后端处理。前端只向 127.0.0.1:8000 发送请求；模型服务的调用由后端配置决定。前端不包含统计追踪、广告脚本或外部字体。</p><h3>连接凭据</h3><p>本地令牌只保存在当前标签页的 sessionStorage，并通过 X-Local-Token 请求头发送。聊天界面记录保留在本次页面会话中；后台记录遵循后端的持久化设置。</p></div>'),
    'mobile-menu': () => { const nav = $('#main-nav'); const open = nav.classList.toggle('open'); element.setAttribute('aria-expanded', String(open)); element.setAttribute('aria-label', open ? '收起导航' : '展开导航'); },
    'new-space': newSpaceModal, 'new-plan': planModal, 'edit-scope': scopeModal,
    'continue-learning': () => currentSpace() ? go('plan') : newSpaceModal(),
    'demo-mode': () => { state.chatController?.abort(); state.mode = 'demo'; state.error = ''; state.pendingJob = null; state.selected = demo.spaces[0]?.id || ''; state.messages.clear(); storageWrite(session, MODE_KEY, 'demo'); closeModal(); render(); toast('已切换到示例体验，所有操作仅影响本地示例。'); },
    refresh: () => { state.graphSpace = ''; state.topics.clear(); state.plans.clear(); return refreshData(); },
    'space-filter': () => { state.spaceFilter = id; render(); },
    'material-filter': () => { state.materialFilter = id; render(); },
    upload: () => $('#file-input')?.click(),
    'view-material': () => viewMaterial(id), 'review-material': () => reviewMaterial(id),
    'publish-graph': async () => { const { materialId, diff } = state.review; await api.publish(materialId, diff.candidate_revision_id, diff.base_graph_version); closeModal(); await refreshData(); toast('知识图谱已发布，可以创建学习空间了。'); },
    'select-node': () => { state.graphSelected = id; $('.graph-stage').querySelectorAll('.graph-node').forEach(n => { n.classList.toggle('selected', n.dataset.id === id); n.setAttribute('aria-pressed', String(n.dataset.id === id)); }); $('#graph-detail').innerHTML = graphDetail(); },
    'zoom-in': () => { state.zoom = Math.min(2.5, state.zoom + .2); graphTransform(); },
    'zoom-out': () => { state.zoom = Math.max(.4, state.zoom - .2); graphTransform(); },
    'zoom-reset': () => { state.zoom = 1; state.pan = { x: 0, y: 0 }; graphTransform(); },
    'reload-graph': () => { state.graphSpace = ''; return state.mode === 'live' ? loadRoute() : render(); },
    'explore-topic': () => { state.graphSelected = id; go('graph'); },
    'ask-node': () => { const node = graphData().nodes.find(n => n.id === id); go('assistant'); setTimeout(() => { if ($('#question')) { $('#question').value = `请用直观的方式解释「${node.title || node.name}」，并给出资料依据。`; $('#question').focus(); } }, 0); },
    'node-source': () => { const node = graphData().nodes.find(n => n.id === id); return showSource(node.source_refs[Number(element.dataset.index)]); },
    'chat-source': () => { const message = state.messages.get(currentSpace().id)[Number(element.dataset.message)]; return showSource(message.citations[Number(element.dataset.index)]); },
    'complete-task': () => completeTask(id), 'toggle-timer': toggleTimer, 'reset-timer': () => { resetTimer(); render(); },
    suggestion: () => { const input = $('#question'); input.value = element.dataset.text; input.focus(); },
    'resume-chat': () => sendChat('', true),
    'stop-chat': async () => { state.chatController?.abort(); const job = state.pendingJob; if (job) { await api.cancelRun(job.run_id); state.pendingJob = null; } updateChat(); toast('已停止接收本次回答。'); },
  };
  if (actions[action]) await busy(element.tagName === 'BUTTON' && !['select-node', 'toggle-timer', 'reset-timer'].includes(action) ? element : null, actions[action]);
});

document.addEventListener('submit', async event => {
  const form = event.target; event.preventDefault();
  const submit = $('button[type=submit]', form);
  await busy(submit, async () => {
    const error = $('[data-form-error]', form); if (error) error.hidden = true;
    if (form.id === 'new-space-form') await createSpace(form);
    if (form.id === 'plan-form') await createPlan(form);
    if (form.id === 'connection-form') {
      const previous = token; token = new FormData(form).get('token').trim();
      try { await api.spaces(); } catch (error) { token = previous; throw error; }
      state.chatController?.abort(); state.mode = 'live'; state.error = ''; state.selected = ''; state.graph = null; state.graphSpace = ''; state.topics.clear(); state.plans.clear(); state.messages.clear(); state.chats.clear(); state.pendingJob = null;
      storageWrite(session, TOKEN_KEY, token); storageWrite(session, MODE_KEY, 'live'); closeModal(); await refreshData(); toast('已连接本地服务，正在使用真实学习数据。');
    }
    if (form.id === 'scope-form') {
      const ids = new FormData(form).getAll('topic_ids'), space = byId(form.dataset.space);
      if (!ids.length) throw new Error('请选择至少一个学习主题。');
      if (state.mode === 'demo') { space.topic_ids = ids; space.space_version++; saveDemo(); }
      else { const result = await api.setScope(space, ids); Object.assign(space, result); state.plans.delete(space.id); }
      closeModal(); render(); toast('学习范围已保存，记得重新安排相关计划。');
    }
    if (form.id === 'chat-form') await sendChat(new FormData(form).get('question').trim());
  });
});
document.addEventListener('input', event => {
  const action = event.target.dataset.input;
  if (action === 'space-search') { state.query = event.target.value; $('#space-results').innerHTML = spaceResults(); }
  if (action === 'material-search') { state.query = event.target.value; $('#material-results').innerHTML = materialResults(); }
  if (action === 'global-search') $('#global-results').innerHTML = searchResults(event.target.value);
});
document.addEventListener('change', async event => {
  if (event.target.id === 'file-input') { try { await uploadFiles(event.target.files); } catch (error) { toast(error.message, true); } }
  if (event.target.dataset.change === 'select-space') {
    if (state.sending) { toast('请先等待当前回答完成，或停止接收。'); event.target.value = state.selected; return; }
    state.selected = event.target.value; state.graphSelected = ''; state.graphSpace = ''; state.graph = null; state.zoom = 1; state.pan = { x: 0, y: 0 }; state.planError = ''; render(); await loadRoute();
  }
});
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchModal(); }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && event.target.id === 'question') { event.preventDefault(); $('#chat-form')?.requestSubmit(); }
  if (event.target.classList?.contains('graph-node') && ['Enter', ' '].includes(event.key)) { event.preventDefault(); event.target.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
  if (event.key === 'ArrowDown' && modal.classList.contains('search-dialog')) {
    const results = [...modal.querySelectorAll('.search-result')]; const next = results[(results.indexOf(document.activeElement) + 1) % results.length]; if (next) { event.preventDefault(); next.focus(); }
  }
  if (event.key === 'ArrowUp' && modal.classList.contains('search-dialog')) {
    const results = [...modal.querySelectorAll('.search-result')]; const next = results[(results.indexOf(document.activeElement) - 1 + results.length) % results.length]; if (next) { event.preventDefault(); next.focus(); }
  }
});
modal.addEventListener('click', event => { if (event.target !== modal) return; const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); });
modal.addEventListener('cancel', () => { modalVersion++; });
document.addEventListener('dragover', event => { const zone = event.target.closest('#upload-zone'); if (zone) { event.preventDefault(); zone.classList.add('dragging'); } });
document.addEventListener('dragleave', event => { const zone = event.target.closest('#upload-zone'); if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove('dragging'); });
document.addEventListener('drop', async event => { const zone = event.target.closest('#upload-zone'); if (zone) { event.preventDefault(); zone.classList.remove('dragging'); try { await uploadFiles(event.dataTransfer.files); } catch (error) { toast(error.message, true); } } });
window.addEventListener('hashchange', () => { state.query = ''; if (modal.open) closeModal(); render(); main.focus({ preventScroll: true }); window.scrollTo(0, 0); void loadRoute(); });
window.addEventListener('beforeunload', () => { state.chatController?.abort(); clearInterval(state.timerInterval); });
state.selected = storageRead(session, `knowpath-selected-${state.mode}`, '') || data().spaces[0]?.id || '';
render();
if (state.mode === 'live') void refreshData();
