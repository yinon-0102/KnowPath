import { icon, logo } from './icons.js';
import { createApi, changeSpace } from './api.js';
import { createLearningController, renderLearning, comparisonPayload } from './learning.js';
import { createWorkspaceController, workspacePath, workspaceFamily, workspaceRoute, dateRange } from './workspace.js';
import { renderWorkspace, updateWorkspaceConditions, correctionFields, workspaceTitles } from './workspace-view.js';
import { DEMO_KEY, MODE_KEY, TOKEN_KEY, esc, uid, dateText, sizeText, storageRead, storageWrite, validateFile, loadDemo, makeDemo, spaceProgress, materialIds, removeSpaceData, graphTitle, buildGraphIndex, graphSlice, createPdfLibrary, pdfKey, validatePdf, sourcePage, pdfLocation } from './store.js';

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
const pdfLibrary = createPdfLibrary();
const pdfUrls = new Set();
let sourceReader = null;
const demo = loadDemo(local);
const state = {
  mode: suppliedToken ? 'live' : storageRead(session, MODE_KEY, 'demo'),
  live: { spaces: [], materials: [], tasks: [] }, loading: false, error: '',
  selected: '', spaceFilter: 'all', materialFilter: 'all', query: '',
  graph: null, graphSpace: '', graphMaterial: '', graphError: '', graphLoading: false, graphSelected: '', zoom: 1, pan: { x: 0, y: 0 },
  graphQuery: '', graphPage: 0, graphScope: 'overview', graphRelation: 'all', graphHistory: [], graphUiKey: '', graphInitialized: false,
  spaceTab: 'overview', spaceTabSpace: '', learningSection: 'diagnostic',
  topics: new Map(), plans: new Map(), planLoading: false, planError: '',
  messages: new Map(), chats: new Map(), chatMaterials: new Map(), pendingJob: null, sending: false, chatController: null,
  timerRemaining: 25 * 60, timerEnd: null, timerInterval: null, timerDuration: 25,
};
const routes = [ ['overview', '概览'], ['spaces', '学习空间'], ['materials', '资料库'], ['assistant', '学习助手'] ];
const statusLabels = { ready: '已就绪', uploaded: '待解析', processing: '解析中', needs_review: '待审核', failed: '解析失败', archived: '已归档', active: '学习中', draft: '待选择范围' };
const taskLabels = { learn: '学习', learning: '学习', review: '复习', practice: '练习', assessment: '测评', retest: '复测' };
const data = () => state.mode === 'demo' ? demo : state.live;
const route = () => {
  const hash = location.hash.slice(1);
  const parts = (hash.startsWith('/') ? hash.slice(1) : hash).split('/');
  let id = '';
  try { id = decodeURIComponent(parts.slice(1).join('/')); } catch { /* Invalid URLs render the missing-space state. */ }
  if (parts[0] === 'materials' && parts[1] === 'graph') {
    try { return { name: 'material-graph', id: decodeURIComponent(parts.slice(2).join('/')) }; } catch { return { name: 'material-graph', id: '' }; }
  }
  return { name: parts[0] || 'overview', id };
};
const currentSpace = () => data().spaces.find(s => s.id === state.selected) || data().spaces[0];
const byId = id => data().spaces.find(s => s.id === id);
const learning = createLearningController({ api, storage: session,
  context: () => ({ mode: state.mode, spaceId: currentSpace()?.id || '', space: currentSpace(), active: (route().name === 'learning' || route().name === 'space' && state.spaceTab === 'assessment') && !state.loading && !state.error }),
  changed: () => { if (route().name === 'learning' || route().name === 'space' && state.spaceTab === 'assessment') render(); },
});
const workspace = createWorkspaceController({ api, storage: session,
  context: () => ({ mode: state.mode, route: route().name === 'manage' ? route().id : '' }),
  changed: () => { if (route().name === 'manage') { const spaceId = workspace.snapshot().data?.space?.id; if (spaceId && byId(spaceId)) state.selected = spaceId; render(); } },
  committed: async (target, content) => {
    state.graphSpace = ''; state.graph = null; state.topics.clear(); state.plans.clear(); learning.clear();
    const [spaces, materials] = await Promise.all([api.spaces(), api.materials()]);
    if (state.mode === 'live') { state.live = { spaces, materials, tasks: [] }; if (!byId(state.selected)) state.selected = spaces[0]?.id || ''; }
  },
  savePdf: async (file, materialId, versionId) => {
    const hash = await validatePdf(file);
    if (!await pdfLibrary.save({ key: pdfKey('live', materialId, versionId), file, hash, verified: true })) throw new Error('PDF 未能持久保存');
  },
  openSource: async (ref, spaceId) => { if (spaceId && byId(spaceId)) state.selected = spaceId; await openSourcePage(ref); },
  download: (blob, filename) => {
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = filename.replace(/[<>:"/\\|?*]/g, '_'); document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  },
  navigate: (hash, details) => {
    if (details?.spaceId) state.selected = details.spaceId;
    const legacy = { '#/plan': 'tasks', '#/assistant': 'assistant', '#/learning': 'assessment' };
    const tab = legacy[hash] || (hash.startsWith('#/learning/') ? 'assessment' : '');
    if (tab) {
      state.spaceTab = tab; state.spaceTabSpace = details?.spaceId || state.selected;
      if (hash.startsWith('#/learning/')) state.learningSection = hash.split('/')[2] || 'diagnostic';
      location.hash = state.spaceTabSpace ? '#/space/' + encodeURIComponent(state.spaceTabSpace) : '#/spaces';
    } else location.hash = hash;
    if (details?.prompt) setTimeout(() => {
      const input = $('#question'); if (input) { input.value = details.prompt + ' 学习主题：' + (details.topicIds || []).map(id => (state.topics.get(details.spaceId) || []).find(t => t.id === id)?.name || id).join('、'); input.focus(); }
    }, 0);
  },
});
const manageLink = (label, page, id, item, cls = 'text-link') => `<a class="${cls}" href="${workspacePath(page, ...[id, item].filter(value => value != null))}">${esc(label)}</a>`;
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
  finally {
    if (element) {
      element.disabled = false; element.removeAttribute('aria-busy');
      if (element.form?.id === 'plan-form') { element.form.querySelectorAll('select, input, button').forEach(control => { control.disabled = false; }); updatePlanPreview(element.form); }
    }
  }
}
function header() {
  const currentRoute = route();
  const current = currentRoute.name === 'manage'
    ? workspaceFamily(workspaceRoute(currentRoute.id).page)
    : currentRoute.name === 'material-graph' ? 'materials'
    : currentRoute.name === 'space' && state.spaceTab === 'assistant' ? 'assistant'
      : currentRoute.name === 'space' ? 'spaces' : currentRoute.name;
  $('#header').innerHTML = `<div class="container header-inner"><a href="#/overview" class="brand" aria-label="KnowPath 首页">${logo}<span>KnowPath</span></a><nav class="main-nav" id="main-nav" aria-label="主导航">${routes.map(([name, label]) => `<a href="#/${name}" class="nav-link ${current === name ? 'active' : ''}" ${current === name ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav><div class="header-actions">${button('', 'search', 'icon-button', 'search', 'aria-label="搜索学习空间和资料" title="搜索（Ctrl / ⌘ K）"')}${button('', 'settings', 'icon-button settings-button', 'settings', 'aria-label="连接与偏好设置"')}${button('我', 'settings', 'avatar', '', 'aria-label="我的设置"')}${button('', 'mobile-menu', 'icon-button mobile-menu', 'menu', 'aria-label="展开导航" aria-expanded="false" aria-controls="main-nav"')}</div></div>`;
  $('#footer').innerHTML = `<div class="container footer-inner"><div class="footer-brand">${logo}<span>KnowPath · 让知识连接，让学习发生。</span></div><div class="footer-links"><span>${state.mode === 'demo' ? '示例数据 · 仅保存在此浏览器' : '本地服务 · 真实学习数据'}</span><button data-action="help">使用指南</button><button data-action="privacy">数据与隐私</button><span>© ${new Date().getFullYear()} KnowPath</span></div></div>`;
}
function subbar() {
  const title = route().name === 'manage' ? routes.find(r => r[0] === workspaceFamily(workspaceRoute(route().id).page))?.[1] || '学习空间' : route().name === 'material-graph' ? '资料库' : route().name === 'overview' ? '概览' : routes.find(r => r[0] === route().name)?.[1] || (route().name === 'assistant' ? '学习助手' : '学习空间');
  return `<div class="subbar"><div class="subbar-title">${icon('layers')}${title}</div><div class="subbar-right"><span class="date-label">${new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())}</span><button class="mode-pill ${state.mode}" data-action="settings" title="${state.mode === 'demo' ? '正在使用内置示例；点击连接真实后端' : '已选择本地后端模式；点击管理连接'}"><span class="mode-dot"></span>${state.mode === 'demo' ? '示例体验' : state.error ? '连接需检查' : '本地空间'}${icon('down')}</button></div></div>`;
}
function pageHeading(title, description, action = '') { return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function spaceSelect(action = 'select-space') { return `<select class="select-control" aria-label="选择学习空间" data-change="${action}">${data().spaces.map(s => `<option value="${esc(s.id)}" ${s.id === currentSpace()?.id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>`; }
function art(theme = 'blue') {
  if (theme === 'sage') return '<div class="code-art" aria-hidden="true"><div class="dots">•••</div><div>def explore(ideas):<br>&nbsp; for idea in ideas:<br>&nbsp;&nbsp; learn(idea)<br>&nbsp; return possibilities</div></div>';
  if (theme === 'lavender') return '<svg class="network-art" viewBox="0 0 210 140" aria-hidden="true"><g stroke="#cfc9df" stroke-width="1.3"><path d="M40 42 100 27 170 47M40 42 105 78 170 47M40 99 105 78 168 110M40 99 104 122 168 110M100 27 170 110M40 42 104 122M40 99 100 27M105 78 168 110"/></g><g fill="#e9e5f2" stroke="#b6adcc" stroke-width="2"><circle cx="40" cy="42" r="12"/><circle cx="40" cy="99" r="12"/><circle cx="100" cy="27" r="12"/><circle cx="105" cy="78" r="15"/><circle cx="104" cy="122" r="10"/><circle cx="170" cy="47" r="12"/><circle cx="168" cy="110" r="12"/></g></svg>';
  return '<div class="matrix-art" aria-hidden="true"><span>[</span><div>1&nbsp; 0&nbsp; 2<br>0&nbsp; 1&nbsp; 3</div><span>]</span></div>';
}
function spaceActions(space) {
  const archived = space.status === 'archived';
  return '<div class="space-card-actions" role="group" aria-label="' + esc(space.name) + '的管理操作">'
    + button(archived ? '恢复学习' : '归档', archived ? 'restore-space' : 'archive-space', 'text-link', archived ? 'refresh' : 'archive', 'data-id="' + esc(space.id) + '"')
    + button('删除', 'delete-space', 'text-link space-action-danger', 'trash', 'data-id="' + esc(space.id) + '"') + '</div>';
}
function spaceChangeModal(spaceId, action) {
  const space = byId(spaceId);
  if (!space) throw new Error('这个学习空间已不存在，请刷新列表。');
  const label = { archive: '归档', restore: '恢复', delete: '删除' }[action];
  if (!label) return;
  const deleting = action === 'delete';
  const description = deleting
    ? '<p class="space-delete-warning">此操作会永久删除该空间的测评、答题记录、学习进度、计划及会话，删除后无法恢复。</p>' + notice('资料库中的原始资料和其他学习空间会保留。')
    : '<p class="modal-intro">' + (action === 'archive' ? '归档后可在“已归档”中查看或恢复。学习记录和资料都会保留。' : '恢复后，该空间会重新出现在“学习中”，可以继续原有学习。') + '</p>';
  openModal(label + '学习空间', '<p class="modal-intro">当前空间：<strong>' + esc(space.name) + '</strong></p>' + description
    + '<form id="space-change-form" data-space="' + esc(space.id) + '" data-name="' + esc(space.name) + '" data-version="' + esc(space.space_version) + '" data-mode="' + state.mode + '" data-operation="' + action + '">'
    + (deleting ? '<label class="check-label space-delete-confirm"><input type="checkbox" name="confirm" required>我已了解，确认永久删除此空间及其学习记录</label>' : '')
    + '<div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">' + button('取消', 'close-modal', 'button button-secondary')
    + '<button type="submit" class="button ' + (deleting ? 'button-danger' : 'button-primary') + '">' + (deleting ? '永久删除' : action === 'archive' ? '确认归档' : '确认恢复') + '</button></div></form>');
}
function forgetSpace(spaceId) {
  if (state.selected === spaceId) resetTimer();
  if (state.pendingJob?.spaceId === spaceId) { state.chatController?.abort(); state.pendingJob = null; state.sending = false; }
  for (const cache of [state.topics, state.plans, state.messages, state.chats]) cache.delete(spaceId);
  if (state.graphSpace === spaceId || state.selected === spaceId) {
    state.graph = null; state.graphSpace = ''; state.graphSelected = ''; state.graphError = '';
  }
  learning.forget(spaceId);
  const plans = storageRead(session, 'knowpath-plan-ids', {});
  if (plans && typeof plans === 'object' && !Array.isArray(plans)) { delete plans[spaceId]; storageWrite(session, 'knowpath-plan-ids', plans); }
}
async function submitSpaceChange(form) {
  const mode = form.dataset.mode, action = form.dataset.operation;
  if (mode !== state.mode) throw new Error('数据模式已变化，请关闭弹窗后重新操作。');
  const space = { id: form.dataset.space, name: form.dataset.name, space_version: Number(form.dataset.version) };
  if (!byId(space.id)) throw new Error('这个学习空间已不存在，请刷新列表。');
  const version = modalVersion;
  let result;
  try { result = await changeSpace({ api, mode, space, action, confirmed: new FormData(form).get('confirm') === 'on' }); }
  catch (error) {
    if (error.code === 'VERSION_CONFLICT') throw new Error('空间已被更新，请取消并刷新页面后重新确认，避免修改过期数据。');
    if (['NETWORK', 'TIMEOUT', 'RUN_PENDING'].includes(error.code)) throw new Error('尚未确认本次操作的结果，请取消并刷新列表核对后再试。');
    throw error;
  }
  const target = mode === 'demo' ? demo : state.live;
  if (action === 'delete') { removeSpaceData(target, space.id); forgetSpace(space.id); }
  else { const existing = target.spaces.find(s => s.id === space.id); if (existing) Object.assign(existing, result); }
  if (mode === 'demo') saveDemo();
  if (state.mode !== mode) return;
  if (!byId(state.selected) || action === 'archive' && state.selected === space.id) {
    state.selected = target.spaces.find(s => s.status !== 'archived')?.id || target.spaces[0]?.id || '';
    storageWrite(session, 'knowpath-selected-' + mode, state.selected);
  }
  if (modal.open && modalVersion === version) closeModal();
  if (action === 'delete' && route().name === 'space' && route().id === space.id) go('spaces');
  else render();
  toast(action === 'delete' ? '学习空间已删除，资料库中的原始资料已保留。' : action === 'archive' ? '空间已归档，可在“已归档”中查看或恢复。' : '空间已恢复，可在“学习中”继续学习。');
}
function spaceCard(space, i) {
  const theme = space.theme || ['blue', 'sage', 'lavender'][i % 3];
  const progress = state.mode === 'demo' ? spaceProgress(space) : null;
  const count = materialIds(space).length;
  return `<article class="space-card"><a class="card-art ${theme}" href="#/space/${encodeURIComponent(space.id)}" aria-label="打开${esc(space.name)}"><span class="art-label">${['让理解更进一步', '给好奇心一个开始', '与新的可能相遇'][i % 3]}</span>${art(theme)}</a><div class="card-content"><div class="card-title-row"><h3><a href="#/space/${encodeURIComponent(space.id)}">${esc(space.name)}</a></h3>${space.status === 'archived' ? '<span class="badge">已归档</span>' : ''}</div><p>${esc(space.goal || '把你的资料，变成自己的知识。')}</p><div class="card-meta"><span>${icon('file')}${count} 份资料</span><span>${icon('graph')}${(space.topic_ids || []).length} 个学习主题</span></div>${progress == null ? '<div class="empty-progress">从资料出发，逐步建立学习记录</div>' : `<div class="progress" role="progressbar" aria-label="${esc(space.name)}示例掌握度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><div class="progress-fill" style="width:${progress}%"></div></div>`}<div class="card-bottom"><span class="subtle">${progress == null ? esc(statusLabels[space.status] || '学习空间') : `已掌握 ${progress}%`}</span>${link('继续探索', `#/space/${encodeURIComponent(space.id)}`)}</div>${spaceActions(space)}</div></article>`;
}
function taskTitle(task) { return task.title || task.name || task.context?.title || `${taskLabels[task.type || task.kind] || '学习'}：${(task.topic_ids || []).map(id => (state.topics.get(task.space_id) || byId(task.space_id)?.nodes || []).find(n => n.id === id)?.title || '知识主题').join('、')}`; }
function taskRow(task) {
  const done = task.status === 'completed';
  const kind = task.type || task.kind || 'learn';
  const taskLinks = state.mode === 'live' && task.plan_id
    ? '<div class="workspace-task-links">' + manageLink(done ? '查看任务' : '开始学习', done ? 'task' : 'session', task.plan_id, task.id, done ? 'text-link' : 'text-link task-start-link') + manageLink('调整', 'task', task.plan_id, task.id) + '</div>'
    : '';
  return `<div class="task-row"><button class="task-check ${done ? 'done' : ''}" data-action="complete-task" data-id="${esc(task.id)}" aria-label="${done ? '已完成' : '完成任务'}：${esc(taskTitle(task))}" aria-pressed="${done}" ${done && state.mode === 'live' ? 'disabled' : ''}>${done ? icon('check') : ''}</button><div class="task-content"><div class="task-title ${done ? 'completed' : ''}">${esc(taskTitle(task))}</div><div class="task-detail">${esc(byId(task.space_id)?.name || currentSpace()?.name || '')} · ${Number(task.minutes || task.estimated_minutes || 25)} 分钟</div></div><span class="task-tag ${kind === 'review' ? 'review' : ''}">${taskLabels[kind] || '学习'}</span>${taskLinks}</div>`;
}
function overview() {
  const spaces = data().spaces.filter(s => s.status !== 'archived');
  const tasks = activeTasks();
  const stats = [
    ['layers', spaces.length, '个', '学习空间'],
    ['file', data().materials.length, '份', '学习资料'],
    ['check', tasks.filter(t => t.status === 'completed').length, '项', '已完成任务']
  ];
  const modules = {
    spaces: ['layers', '整理目标，继续学习'],
    materials: ['file', '导入和管理学习资料'],
    assistant: ['spark', '围绕资料随时提问'],
  };
  return `<div class="overview-page"><section class="hero" aria-labelledby="hero-title"><div class="hero-copy"><div class="hero-overline">${icon('spark')}每一点好奇，都值得继续。</div><h1 id="hero-title">让每一步学习，<br>都有方向。</h1><p class="hero-description">把零散的资料，连成清晰的知识。<br>在属于你的节奏里，让理解自然发生。</p><div class="hero-actions">${button('继续学习', 'continue-learning', 'button button-primary', 'arrow')}${link('进入学习空间', '#/spaces')}</div></div><img class="hero-art" src="./assets/knowledge-orbit.svg" alt="知识围绕学习目标连接，形成清晰的探索路径" width="640" height="500"><div class="hero-caption">每个知识点，都能找到彼此。</div></section>
    <div class="stats-strip overview-stats" aria-label="${state.mode === 'demo' ? '示例学习统计' : '学习统计'}">
      ${stats.map(([glyph, value, unit, label]) => `<div class="stat"><span class="stat-icon">${icon(glyph)}</span><div><div class="stat-value">${value}<small>${unit}</small></div><div class="stat-label">${label}</div></div></div>`).join('')}
    </div>
    <section class="overview-navigation" aria-labelledby="overview-navigation-title">
      <h2 id="overview-navigation-title">学习入口</h2>
      <nav class="overview-modules" aria-label="学习功能">
        ${routes.filter(([name]) => modules[name]).map(([name, label]) => {
          const [glyph, description] = modules[name];
          return `<a class="overview-module" href="#/${name}" aria-labelledby="module-${name}-title" aria-describedby="module-${name}-description"><span class="overview-module-icon">${icon(glyph)}</span><span class="overview-module-copy"><span class="overview-module-title" id="module-${name}-title">${label}</span><span class="overview-module-description" id="module-${name}-description">${description}</span></span><span class="overview-module-arrow">${icon('arrow')}</span></a>`;
        }).join('')}
      </nav>
    </section>
  </div>`;
}
function filteredSpaces() {
  return data().spaces.filter(s => (state.spaceFilter === 'all' || (state.spaceFilter === 'active' ? s.status !== 'archived' : s.status === 'archived')) && `${s.name} ${s.goal || ''}`.toLowerCase().includes(state.query.toLowerCase()));
}
function spacesPage() {
  return `${pageHeading('学习空间', '每一个目标，都值得一个专属空间。', button('新建空间', 'new-space', 'button button-primary', 'plus'))}<div class="toolbar"><div class="filter-group" role="group" aria-label="学习空间筛选">${[['all', '全部空间'], ['active', '学习中'], ['archived', '已归档']].map(([id, label]) => `<button class="filter-button ${state.spaceFilter === id ? 'active' : ''}" data-action="space-filter" data-id="${id}" aria-pressed="${state.spaceFilter === id}">${label}</button>`).join('')}</div><label class="search-field">${icon('search')}<input type="search" data-input="space-search" placeholder="搜索你的学习空间" aria-label="搜索你的学习空间" value="${esc(state.query)}"></label></div><div id="space-results">${spaceResults()}</div>`;
}
function spaceResults() {
  const spaces = filteredSpaces();
  if (spaces.length) return '<div class="space-grid">' + spaces.map(spaceCard).join('') + '</div>';
  if (state.query) return empty('没有找到相关空间', '换一个关键词试试，或清空搜索条件。');
  if (state.spaceFilter === 'archived') return empty('暂无已归档空间', '点击空间卡片上的“归档”即可收纳暂时不学的空间，之后可以随时恢复。', button('查看全部空间', 'space-filter', 'button button-secondary', '', 'data-id="all"'));
  if (state.spaceFilter === 'active' && data().spaces.length) return empty('暂无学习中的空间', '可以新建空间，或在“已归档”中恢复已有空间。', button('查看已归档', 'space-filter', 'button button-secondary', '', 'data-id="archived"'));
  return empty('这里还没有学习空间', '导入资料，给自己的学习目标一个开始。', button('创建学习空间', 'new-space', 'button button-primary', 'plus'));
}
function normalizeType(material) { const type = String(material.type || material.name.split('.').pop()).toLowerCase(); return type.includes('pdf') ? 'pdf' : type.includes('mark') || type === 'md' ? 'md' : 'txt'; }
function materialResults() {
  const materials = data().materials.filter(m => (state.materialFilter === 'all' || normalizeType(m) === state.materialFilter) && m.name.toLowerCase().includes(state.query.toLowerCase()));
  if (!materials.length) return empty('还没有找到资料', state.query ? '试试其他文件名，或者清空筛选条件。' : '你的教材、笔记和灵感，都可以从这里开始。', button('选择文件', 'upload', 'button button-primary', 'upload'), 'file');
  return `<div class="material-list"><div class="material-head"><span>资料名称</span><span>状态</span><span class="material-size">文件大小</span><span class="material-date">导入日期</span><span class="material-action">操作</span></div>${materials.map(m => `<div class="material-row"><button class="material-name" data-action="view-material" data-id="${esc(m.id)}"><span class="file-icon ${normalizeType(m)}"><span class="file-type">${normalizeType(m).toUpperCase()}</span></span><span title="${esc(m.name)}">${esc(m.name)}</span></button><span class="material-status"><span class="status-tag ${m.status !== 'ready' ? 'pending' : ''}">${statusLabels[m.status] || esc(m.status)}</span></span><span class="material-size">${sizeText(m.size_bytes)}</span><span class="material-date">${dateText(m.created_at)}</span><span class="material-action">${button('知识结构', 'material-graph', 'text-link', 'graph', `data-id="${esc(m.id)}"`)}${button('查看', 'view-material', 'text-link', '', `data-id="${esc(m.id)}"`)}${state.mode === 'live' ? manageLink('管理', 'material', m.id) : ''}</span></div>`).join('')}</div>`;
}
function materialsPage() {
  if (route().name === 'material-graph') {
    state.graphMaterial = route().id;
    state.graphSpace = '';
    return `<a class="text-link back-link" href="#/materials">返回资料库</a>${graphPage({ materialLibrary: true })}`;
  }
  return `${pageHeading('资料库', '让读过的每一页，都成为自己的知识。', button('刷新', 'refresh', 'button button-secondary', 'refresh'))}<div class="upload-zone" id="upload-zone"><div class="upload-symbol">${icon('upload')}</div><div><h2>新的知识，从一份资料开始。</h2><p>拖拽文件到这里，支持 PDF、Markdown、TXT，上传后自动生成知识图谱</p></div><label class="button button-secondary file-picker">${icon('plus')}选择文件<input id="file-input" type="file" accept=".pdf,.md,.txt" aria-label="选择学习资料" multiple></label></div>${state.mode === 'demo' ? notice('示例模式下，文本和 PDF 可在本地阅读；PDF 知识解析需要连接本地服务。') : ''}<div class="toolbar"><div class="filter-group" role="group" aria-label="资料类型筛选">${[['all', '全部资料'], ['pdf', 'PDF'], ['md', 'Markdown'], ['txt', 'TXT']].map(([id, label]) => `<button class="filter-button ${state.materialFilter === id ? 'active' : ''}" data-action="material-filter" data-id="${id}" aria-pressed="${state.materialFilter === id}">${label}</button>`).join('')}</div><label class="search-field">${icon('search')}<input type="search" data-input="material-search" placeholder="搜索资料名称" aria-label="搜索资料名称" value="${esc(state.query)}"></label></div><div id="material-results">${materialResults()}</div>`;
}
function graphMaterial() {
  const explicitId = state.graphMaterial || (['graph', 'material-graph'].includes(route().name) ? route().id : '');
  return data().materials.find(material => material.id === explicitId);
}
function graphSpace() { return data().spaces.find(space => space.id === state.graphSpace && space.status !== 'archived'); }
function demoMaterialGraph(material) {
  if (!material) return { nodes: [], edges: [] };
  const headings = [...(material.text || '').matchAll(/^#{1,3}\s+(.+)$/gm)].map(match => match[1].trim()).filter(Boolean);
  const names = headings.length ? headings : [material.name.replace(/\.(pdf|md|txt)$/i, '')];
  const nodes = names.map((name, index) => ({ id: `material-${material.id}-${index}`, title: name, name, status: 'new', level: 1, description: '从已上传资料中提取的知识主题。', source_refs: [{ material_id: material.id, material_version_id: material.current_version_id || material.id }] }));
  return { nodes, edges: nodes.slice(1).map((node, index) => ({ id: `material-edge-${material.id}-${index}`, from_id: nodes[0].id, to_id: node.id, type: 'contains' })) };
}
function graphContext() {
  const material = graphMaterial();
  const space = graphSpace();
  if (material) return { type: 'material', id: material.id, material };
  if (space) return { type: 'space', id: space.id, space };
  const firstMaterial = data().materials.find(item => item.status !== 'archived');
  if (firstMaterial) return { type: 'material', id: firstMaterial.id, material: firstMaterial };
  return currentSpace() ? { type: 'space', id: currentSpace().id, space: currentSpace() } : null;
}
function graphData() {
  const context = graphContext();
  if (context?.type === 'material') return state.mode === 'demo' ? demoMaterialGraph(context.material) : state.graph || { nodes: [], edges: [] };
  return state.mode === 'demo' ? { nodes: context?.space?.nodes || [], edges: context?.space?.edges || [] } : state.graph || { nodes: [], edges: [] };
}
let graphIndexCache;
function graphModel() {
  const graph = graphData();
  if (!graphIndexCache || graphIndexCache.nodes !== graph.nodes || graphIndexCache.edges !== graph.edges) {
    graphIndexCache = { nodes: graph.nodes, edges: graph.edges, value: buildGraphIndex(graph) };
  }
  return graphIndexCache.value;
}
function graphNodeButton(entry, focused = false) {
  const index = graphModel(), node = entry.node, label = graphTitle(node);
  const relationships = entry.labels?.length ? entry.labels.join(' / ') : '';
  const status = state.mode === 'demo' ? ({ mastered: '已掌握', learning: '学习中', new: '待探索' })[node.status] : '';
  return `<button type="button" class="knowledge-node ${focused ? 'knowledge-node-focus' : ''}" data-action="select-node" data-id="${esc(node.id)}" title="${esc(label)}" aria-label="${esc(focused ? '当前主题：' + label : '查看主题：' + label)}" ${focused ? 'aria-current="true"' : ''}>
    <span class="knowledge-node-kind">${focused ? '当前主题' : relationships || (node.level ? '第 ' + Number(node.level) + ' 层主题' : '知识主题')}</span>
    <span class="knowledge-node-title">${esc(label)}</span>
    <span class="knowledge-node-meta">${index.adjacent.get(node.id)?.size || 0} 个关联主题${status ? ' · ' + status : ''}${focused ? '' : '<span aria-hidden="true">' + icon('chevron') + '</span>'}</span>
  </button>`;
}
function graphDetail() {
  const index = graphModel(), node = index.byId.get(state.graphSelected);
  if (!node) return `<div class="graph-detail-placeholder">${icon('graph')}<h2>从一个主题开始</h2><p>点击左侧主题，查看完整内容、资料来源和它的直接关联。</p><p>也可以搜索标题或关键词，在全部 ${index.nodes.length} 个知识点中定位。</p></div>`;
  const title = graphTitle(node), refs = node.source_refs || [];
  const description = node.description || '这个主题暂时没有单独的说明，可以查看资料来源。';
  return `<div class="graph-detail-heading"><span class="badge">主题详情</span></div>
    <h2 class="graph-readable-title" title="${esc(title)}">${esc(title)}</h2>
    ${title.length > 42 ? '<details class="graph-full-text"><summary>阅读完整标题</summary><p>' + esc(title) + '</p></details>' : ''}
    <p class="graph-readable-description">${esc(description)}</p>
    ${description.length > 160 ? '<details class="graph-full-text"><summary>展开完整说明</summary><p>' + esc(description) + '</p></details>' : ''}
    <dl class="graph-facts"><div><dt>${state.mode === 'demo' ? '示例学习状态' : '主题状态'}</dt><dd>${({ mastered: '已掌握', learning: '学习中', new: '待探索', active: '有效主题', pending: '待确认' })[node.status] || '待探索'}</dd></div><div><dt>直接关联</dt><dd>${index.adjacent.get(node.id)?.size || 0} 个主题</dd></div></dl>
    <div class="graph-source-heading">资料来源 <span>${refs.length}</span></div>
    ${refs.length ? '<div class="graph-source-list">' + refs.map((ref, i) => '<div class="graph-source-item"><button class="citation" data-action="node-source" data-id="' + esc(node.id) + '" data-index="' + i + '" title="在新标签页打开资料来源；已关联 PDF 时直接打开文档">' + icon('file') + esc(data().materials.find(m => m.id === ref.material_id)?.name || '查看来源 ' + (i + 1)) + '</button><button class="text-link source-text-link" data-action="node-source-text" data-id="' + esc(node.id) + '" data-index="' + i + '">新页阅读来源</button></div>').join('') + '</div>' : '<p class="subtle">暂无可查看的来源片段。</p>'}
    ${button('围绕这个主题提问', 'ask-node', 'button button-soft', 'spark', 'data-id="' + esc(node.id) + '"')}`;
}
function graphExplorer() {
  const index = graphModel(), view = graphSlice(index, { focusId: state.graphSelected, query: state.graphQuery, page: state.graphPage, scope: state.graphScope, relation: state.graphRelation });
  state.graphPage = view.page;
  const heading = view.search ? '搜索结果' : view.focus ? '直接关联' : state.graphScope === 'all' ? '全部知识' : '主题总览';
  const hint = view.search ? '在全部知识点的标题和说明中搜索' : view.focus ? '每次只展开一层，点击关联主题继续探索' : '先选一个主题，再逐层查看它的关联';
  const filters = [['all', '全部关联'], ['children', '下级主题'], ['parents', '上级主题'], ['prerequisites', '前置知识'], ['other', '其他关系']];
  const scene = view.focus
    ? `<div class="graph-neighborhood ${view.items.length ? '' : 'graph-neighborhood-empty'}"><div class="graph-focus-column">${graphNodeButton({ node: view.focus }, true)}</div><div class="graph-neighbors">${view.items.length ? view.items.map(entry => '<div class="graph-neighbor">' + graphNodeButton(entry) + '</div>').join('') : '<div class="graph-no-neighbors"><h3>' + (state.graphRelation === 'all' ? '暂无直接关联' : '没有这一类关联') + '</h3><p>' + (state.graphRelation === 'all' ? '当前资料没有提供这个主题的关联关系。可以阅读右侧来源，或返回总览继续浏览。' : '切换到“全部关联”查看其他关系。') + '</p></div>'}</div></div>`
    : view.items.length ? '<div class="graph-topic-grid">' + view.items.map(entry => graphNodeButton(entry)).join('') + '</div>'
      : '<div class="graph-no-results">' + icon('search') + '<h3>没有找到相关主题</h3><p>试试更短的关键词，或清空搜索查看主题总览。</p>' + button('清空搜索', 'graph-clear-search', 'button button-secondary') + '</div>';
  return `<div class="graph-browser-layout">
    <section class="graph-browser-stage" aria-label="知识主题浏览">
      <div class="graph-browser-heading"><div><h2 id="graph-view-title" tabindex="-1">${heading}</h2><p>${hint}</p></div>${view.focus ? '<label class="graph-relation-label"><span class="screenreader">筛选关联类型</span><select class="select-control" aria-label="筛选关联类型" data-change="graph-relation">' + filters.map(([value,label]) => '<option value="' + value + '" ' + (state.graphRelation === value ? 'selected' : '') + '>' + label + '</option>').join('') + '</select></label>' : '<span class="graph-total">' + view.total + ' 个主题</span>'}</div>
      ${scene}
      <div class="graph-pagination"><span role="status">${view.total ? '显示 ' + (view.page * view.pageSize + 1) + '–' + Math.min((view.page + 1) * view.pageSize, view.total) + ' / ' + view.total + ' 个' + (view.focus ? '关联主题' : '主题') : '0 个主题'}</span><div>${button('上一页', 'graph-prev', 'icon-button graph-page-button', '', 'aria-label="上一页主题" ' + (view.page === 0 ? 'disabled' : ''))}<span>${view.page + 1} / ${view.pages}</span>${button('下一页', 'graph-next', 'icon-button graph-page-button', '', 'aria-label="下一页主题" ' + (view.page + 1 >= view.pages ? 'disabled' : ''))}</div></div>
    </section>
    <aside class="panel graph-browser-detail" id="graph-detail" aria-label="知识主题详情">${graphDetail()}</aside>
  </div>`;
}
function updateGraphExplorer(focusHeading = false) {
  const root = $('#graph-explorer');
  if (!root) return;
  root.innerHTML = graphExplorer();
  const input = $('[data-input="graph-search"]'); if (input && input.value !== state.graphQuery) input.value = state.graphQuery;
  const back = $('[data-action="graph-back"]'); if (back) back.disabled = !state.graphHistory.length;
  for (const scope of ['overview', 'all']) {
    const control = $('[data-action="graph-' + scope + '"]');
    if (control) control.setAttribute('aria-pressed', String(!state.graphSelected && !state.graphQuery && state.graphScope === scope));
  }
  if (focusHeading) $('#graph-view-title')?.focus({ preventScroll: true });
}
function selectGraphNode(id) {
  if (!graphModel().byId.has(id)) return;
  if (state.graphSelected !== id || state.graphQuery) state.graphHistory.push({ id: state.graphSelected, page: state.graphPage, query: state.graphQuery, scope: state.graphScope, relation: state.graphRelation });
  state.graphHistory = state.graphHistory.slice(-50);
  state.graphSelected = id; state.graphQuery = ''; state.graphPage = 0; state.graphRelation = 'all';
  updateGraphExplorer(true);
}
function graphContextPicker(context) {
  const spaces = data().spaces.filter(item => item.status !== 'archived');
  const materials = data().materials.filter(item => item.status !== 'archived');
  const selected = `${context.type}:${context.id}`;
  return `<label class="graph-material-picker"><span>查看范围</span><select class="select-control" aria-label="选择知识图谱查看范围" data-change="select-graph-context">${spaces.length ? `<optgroup label="学习空间">${spaces.map(item => `<option value="space:${esc(item.id)}" ${selected === 'space:' + item.id ? 'selected' : ''}>${esc(item.name)} · 已绑定资料</option>`).join('')}</optgroup>` : ''}<optgroup label="已上传资料">${materials.map(item => `<option value="material:${esc(item.id)}" ${selected === 'material:' + item.id ? 'selected' : ''}>${esc(item.name)}${item.status !== 'ready' ? ' · ' + (statusLabels[item.status] || item.status) : ''}</option>`).join('')}</optgroup></select></label>`;
}
function graphPage({ embedded = false, materialLibrary = false } = {}) {
  const heading = embedded ? '' : pageHeading(materialLibrary ? '知识结构' : '知识图谱', materialLibrary ? '按资料浏览主题和关联，上传后即可查看。' : '从一个主题出发，逐步看清知识之间的联系。');
  const context = graphContext();
  if (!context) return heading + empty('先上传一份资料', '资料上传后会自动生成知识图谱，不需要先创建学习空间。', link('前往资料库', '#/materials', 'button button-primary', 'upload'), 'graph');
  if (context.type === 'material' && state.graphMaterial !== context.id) state.graphMaterial = context.id;
  if (context.type === 'space' && state.graphSpace !== context.id) state.graphSpace = context.id;
  const material = context.type === 'material' ? context.material : null;
  const index = graphModel(), key = state.mode + ':' + context.type + ':' + context.id + ':' + (material?.current_version_id || material?.version || context.space?.space_version || '');
  if (state.graphUiKey !== key) {
    state.graphUiKey = key; state.graphQuery = ''; state.graphPage = 0; state.graphHistory = []; state.graphScope = 'overview'; state.graphRelation = 'all'; state.graphInitialized = false;
    if (!index.byId.has(state.graphSelected)) state.graphSelected = '';
  }
  if (index.nodes.length && !state.graphInitialized) {
    if (!state.graphSelected && index.roots.length === 1 && index.adjacent.get(index.roots[0].id)?.size) state.graphSelected = index.roots[0].id;
    state.graphInitialized = true;
  }
  if (state.graphSelected && index.nodes.length && !index.byId.has(state.graphSelected)) state.graphSelected = '';
  const toolbar = embedded ? '' : `<div class="toolbar graph-space-toolbar">${materialLibrary ? `<span class="badge">当前资料：${esc(material?.name || '资料')}</span>` : graphContextPicker(context)}<div class="workspace-links">${materialLibrary ? link('返回资料库', '#/materials') : state.mode === 'live' && context.type === 'space' ? manageLink('知识更新', 'knowledge-updates', context.space.id) + manageLink('知识纠错', 'knowledge-correct', context.space.id) + manageLink('变更记录', 'knowledge-changes', context.space.id) : state.mode === 'live' ? manageLink('管理资料', 'material', material.id) : ''}${button('重新加载', 'reload-graph', 'button button-secondary', 'refresh')}</div></div>`;
  const content = state.graphLoading ? '<div class="skeleton" role="status" aria-label="正在加载知识图谱"></div>' : index.nodes.length ? `
    <div class="graph-browser-controls"><div class="graph-browse-buttons">${button('返回上层', 'graph-back', 'button button-secondary', '', state.graphHistory.length ? '' : 'disabled')}${button('主题总览', 'graph-overview', 'button button-secondary', '', 'aria-pressed="' + (!state.graphSelected && !state.graphQuery && state.graphScope === 'overview') + '"')}${button('全部知识', 'graph-all', 'button button-secondary', '', 'aria-pressed="' + (!state.graphSelected && !state.graphQuery && state.graphScope === 'all') + '"')}</div><label class="search-field graph-search">${icon('search')}<input type="search" data-input="graph-search" placeholder="搜索全部 ${index.nodes.length} 个知识点" aria-label="搜索全部知识点" value="${esc(state.graphQuery)}"></label></div>
    <div id="graph-explorer">${graphExplorer()}</div>
    <p class="graph-browser-note">共 ${index.nodes.length} 个知识点 · ${index.edges.length} 条关系。节点过多时分页浏览，完整标题和原文可在详情中查看。</p>
  ` : state.mode === 'live' && material && ['uploaded', 'processing'].includes(material.status)
    ? empty('正在生成知识图谱', '资料已上传，后台正在解析内容。页面会自动刷新，完成后即可查看主题和关系。', button('重新检查', 'reload-graph', 'button button-soft', 'refresh'), 'graph')
    : empty('当前资料还没有可展示的知识点', '请检查资料解析与图谱发布状态；如果刚完成上传，稍等片刻后重新检查。', link('前往资料库', '#/materials', 'button button-soft'), 'graph');
  return heading + toolbar + (state.graphError ? notice(esc(state.graphError), 'error') : '') + content;
}

function weekStrip() {
  const today = new Date(), monday = new Date(today); monday.setDate(today.getDate() - (today.getDay() + 6) % 7);
  return `<div class="week-strip" aria-label="本周日期">${['一', '二', '三', '四', '五', '六', '日'].map((label, i) => { const date = new Date(monday); date.setDate(monday.getDate() + i); const current = date.toDateString() === today.toDateString(); return `<div class="day ${current ? 'today' : ''}" ${current ? 'aria-current="date"' : ''}>周${label}<strong>${date.getDate()}</strong></div>`; }).join('')}</div>`;
}
function timerText() { const remaining = state.timerEnd ? Math.max(0, Math.ceil((state.timerEnd - Date.now()) / 1000)) : state.timerRemaining; return `${Math.floor(remaining / 60).toString().padStart(2, '0')}:${(remaining % 60).toString().padStart(2, '0')}`; }
function focusCard() { return `<aside class="panel focus-card">${icon('leaf')}<h2>专注这一刻</h2><p>给自己 ${state.timerDuration} 分钟，不急不躁。</p><div class="timer" id="timer" role="timer" aria-label="专注倒计时">${timerText()}</div><div class="timer-actions">${button(state.timerEnd ? '暂停专注' : state.timerRemaining === 0 ? '再来一次' : '开始专注', 'toggle-timer', 'button button-primary', state.timerEnd ? 'pause' : 'play')}${button('', 'reset-timer', 'icon-button', 'refresh', 'aria-label="重置专注计时器"')}</div><div class="timer-note">计时器仅帮助安排时间，<br>完成学习任务需要你亲自确认。</div></aside>`; }
function planPage({ embedded = false } = {}) {
  const space = currentSpace(), tasks = activeTasks().filter(t => !space || t.space_id === space.id);
  const plan = state.mode === 'live' ? state.plans.get(space?.id) : null;
  const requested = Number(plan?.config?.session_count || 0);
  const completed = tasks.filter(t => t.status === 'completed').length;
  const planNote = plan && requested && tasks.length < requested
    ? notice(`本次设置为最多 ${requested} 次，当前学习范围按知识状态生成了 ${tasks.length} 个任务。任务按主题去重，完成后重新安排即可进入下一轮。`, 'warning')
    : plan ? `<div class="plan-summary"><span><b>${tasks.length}</b> 个任务</span><span>每次约 <b>${Number(plan.config?.minutes_per_session || 0)}</b> 分钟</span><span>${plan.config?.include_review ? '包含温习安排' : '未安排温习'}</span></div>` : '';
  const heading = embedded ? '' : pageHeading('学习安排', '按自己的节奏，积累看得见的进步。', button('安排学习', 'new-plan', 'button button-primary', 'plus'));
  if (!space) return heading + empty('学习，从一个小目标开始', '先创建学习空间，再为它安排学习任务。', button('新建空间', 'new-space', 'button button-primary', 'plus'), 'calendar');
  const controls = embedded
    ? `<div class="space-panel-toolbar"><span class="subtle">${completed} / ${tasks.length} 项已完成</span>${button('生成学习任务', 'new-plan', 'button button-primary', 'plus')}</div>`
    : `<div class="toolbar">${spaceSelect()}<span class="subtle">${completed} / ${tasks.length} 项已完成</span></div>`;
  return `${heading}${controls}${state.planError ? notice(esc(state.planError), 'error') : ''}${planNote}<div class="plan-layout"><div>${state.planLoading ? '<div class="skeleton" role="status" aria-label="正在加载学习任务"></div>' : tasks.length ? `<section class="panel plan-list"><div class="panel-title"><h2>从一个任务开始</h2><span class="badge">${state.mode === 'demo' ? '示例任务' : '当前任务'}</span></div>${tasks.map(taskRow).join('')}</section>` : empty('先生成一组学习任务', '学习范围确定后，按照主题和掌握状态生成下一步。', button('生成学习任务', 'new-plan', 'button button-primary', 'calendar'), 'calendar')}</div>${focusCard()}</div>`;
}
function chatMessages() {
  return (state.messages.get(currentSpace()?.id) || []).map((message, index) => `<div class="chat-message ${message.role}"><span class="chat-message-label">${message.role === 'user' ? '你' : state.mode === 'demo' ? '学习助手 · 本地示例资料摘录' : 'KnowPath 学习助手'}</span>${esc(message.text)}${(message.citations || []).map((ref, i) => `<button class="citation" data-action="chat-source" data-message="${index}" data-index="${i}">${esc(ref.material_name || data().materials.find(m => m.id === ref.material_id)?.name || '来源资料')} · 查看依据 ${i + 1}</button>`).join('')}</div>`).join('');
}
function assistantPage({ embedded = false } = {}) {
  const heading = embedded ? '' : pageHeading('学习助手', '带着问题出发，带着理解回来。');
  if (!currentSpace()) return heading + empty('为提问选择一份知识背景', '创建学习空间后，助手才能在你的资料范围内查找依据。', button('新建空间', 'new-space', 'button button-primary', 'plus'), 'spark');
  const pending = state.pendingJob?.spaceId === currentSpace().id;
  return `${heading}<div class="assistant-shell">${embedded ? '' : `<div class="toolbar">${spaceSelect()}<span class="badge">${state.mode === 'demo' ? '本地摘录演示' : '基于你的资料'}</span></div>`}${state.mode === 'demo' ? notice('当前展示本地资料摘录，不调用 AI 模型。连接服务后可使用带来源引用的真实问答。') : ''}<div class="assistant-intro"><div class="assistant-mark">${icon('spark')}</div><h2>今天，想弄懂什么？</h2><p>不必一次问得完美。从一个小小的好奇开始。</p><div class="suggestions">${['帮我梳理这份资料的重点', '如何理解这个知识点？', '接下来可以学习什么？'].map(text => `<button class="suggestion" data-action="suggestion" data-text="${esc(text)}">${text}</button>`).join('')}</div></div><div class="chat-messages" id="chat-messages" aria-live="polite" aria-relevant="additions">${chatMessages()}</div><div id="chat-status" class="notice" role="status" ${!state.sending && !pending ? 'hidden' : ''}>${state.sending ? '正在检索资料与生成回答…' : '上次回答尚未接收完成，可继续接收同一个任务。'}</div>${state.mode === 'live' ? '<details class="workspace-chat-filter"><summary>限定本次问答的资料（默认全部）</summary>' + data().materials.filter(m => materialIds(currentSpace()).includes(m.id)).map(m => '<label class="check-label"><input type="checkbox" data-chat-material value="' + esc(m.id) + '" ' + ((state.chatMaterials.get(currentSpace().id) || []).includes(m.id) ? 'checked' : '') + (state.sending ? ' disabled' : '') + '>' + esc(m.name) + '</label>').join('') + '</details>' : ''}<form id="chat-form" class="chat-form"><label class="screenreader" for="question">输入你的学习问题</label><textarea id="question" name="question" rows="2" maxlength="8000" placeholder="例如：能用直观的方式解释一下线性变换吗？" required ${state.sending ? 'disabled' : ''}></textarea><div class="chat-form-bottom"><span>回答有迹可循，来源随时可查<br>Ctrl / ⌘ + Enter 发送</span>${state.sending ? button('停止', 'stop-chat', 'button button-secondary', 'pause') : pending ? button('继续接收', 'resume-chat', 'button button-primary', 'refresh') : '<button type="submit" class="button button-primary">发送问题' + icon('arrow') + '</button>'}</div></form><p class="chat-footnote">${state.mode === 'demo' ? '示例仅用于体验交互，不代表 AI 生成效果。' : '模型回答可能存在偏差，请结合原始资料核对。'}</p></div>`;
}
function spaceTabButton(tab, label, glyph = '') {
  return `<button type="button" class="space-tab ${state.spaceTab === tab ? 'active' : ''}" data-action="space-tab" data-tab="${tab}">${glyph ? icon(glyph) : ''}${label}</button>`;
}
function spaceOverviewPanel(space, materials, topics) {
  return `<div class="space-overview-grid"><section class="panel detail-hero"><span class="badge">${state.mode === 'demo' ? '示例空间' : '个人学习空间'}</span><h2>${esc(space.name)}</h2><p>${esc(space.goal || '从一个明确目标开始，把资料逐步变成自己的知识。')}</p><div class="space-overview-actions">${button('选择学习范围', 'space-tab', 'button button-primary', 'settings', 'data-tab="scope"')}${button('生成学习任务', 'space-tab', 'button button-secondary', 'calendar', 'data-tab="tasks"')}</div></section><aside class="panel"><div class="panel-title"><h2>空间资料</h2><span class="badge">${materials.length} 份</span></div>${materials.map(m => `<div class="list-item">${icon('file')}<span>${esc(m.name)}</span>${button('查看', 'view-material', 'text-link', '', `data-id="${esc(m.id)}"`)}</div>`).join('') || '<p class="subtle">暂无已绑定资料。</p>'}</aside></div><section class="panel space-summary-panel"><div class="panel-title"><div><h2>当前学习范围</h2><p class="subtle">先选择主题，再生成任务、提问和测评。</p></div><span class="badge">${Number(space.topic_ids?.length || 0)} 个主题</span></div>${topics ? `<div class="topic-list">${topics.filter(topic => (space.topic_ids || []).includes(topic.id)).map(t => `<span class="topic-chip">${esc(t.title || t.name)}</span>`).join('') || '<p class="subtle">还没有选择主题，点击“选择学习范围”开始。</p>'}</div>` : '<p class="subtle">正在读取资料中的知识主题…</p>'}</section>`;
}
function spacePage(spaceId) {
  const space = byId(spaceId);
  if (!space) return pageHeading('学习空间', '每一个目标，都值得一个专属空间。') + empty('没有找到这个空间', '它可能已被移除，或当前数据模式已发生变化。', link('返回学习空间', '#/spaces', 'button button-soft'));
  state.selected = space.id;
  if (state.spaceTabSpace !== space.id) { state.spaceTab = 'overview'; state.spaceTabSpace = space.id; }
  const materials = data().materials.filter(m => materialIds(space).includes(m.id));
  const topics = state.mode === 'demo' ? space.nodes : state.topics.get(space.id);
  const tab = state.spaceTab;
  const body = tab === 'overview' ? spaceOverviewPanel(space, materials, topics)
    : tab === 'scope' ? `<section class="panel"><div class="panel-title"><div><h2>选择学习范围</h2><p class="subtle">纳入想学习的主题，暂时排除不需要的前置内容，再生成任务。</p></div>${button('调整范围', 'edit-scope', 'button button-primary', 'settings')}</div>${topics ? `<div class="scope-summary"><div><span class="scope-summary-label">本次学习</span><div class="topic-list">${topics.filter(topic => (space.topic_ids || []).includes(topic.id)).map(t => `<span class="topic-chip">${esc(t.title || t.name)}</span>`).join('') || '<p class="subtle">当前还没有学习主题。</p>'}</div></div><div><span class="scope-summary-label">暂时排除</span><div class="topic-list">${topics.filter(topic => (space.excluded_topic_ids || []).includes(topic.id)).map(t => `<span class="topic-chip topic-chip-muted">${esc(t.title || t.name)}</span>`).join('') || '<p class="subtle">没有排除主题。</p>'}</div></div></div><p class="subtle scope-prerequisite-note">${space.include_prerequisites === false ? '不会自动纳入前置知识。' : '会自动纳入必要的前置知识。'}</p>` : '<p class="subtle">正在读取知识主题…</p>'}${!space.topic_ids?.length ? notice('保存学习范围后，就可以生成学习任务。', 'warning') : ''}</section>`
    : tab === 'tasks' ? planPage({ embedded: true })
      : tab === 'assistant' ? `<section class="panel embedded-assistant-panel"><div class="panel-title"><div><h2>学习助手</h2><p class="subtle">围绕当前空间的资料提问，回答会附带来源。</p></div></div>${assistantPage({ embedded: true })}</section>`
        : `<div class="embedded-learning-panel">${renderLearning({ mode: state.mode, space, state: learning.snapshot(), spaceSelectHtml: '', section: state.learningSection, embedded: true })}</div>`;
  return `<a class="text-link back-link" href="#/spaces">返回学习空间</a>${pageHeading(esc(space.name), '从资料到理解，所有学习动作都在这个空间完成。', '<div class="space-detail-actions">' + (state.mode === 'live' ? manageLink('空间管理', 'space-settings', space.id, null, 'button button-secondary') : '') + spaceActions(space) + '</div>')}<nav class="space-tabs" aria-label="空间学习功能">${spaceTabButton('overview', '空间概览', 'layers')}${spaceTabButton('scope', '学习范围', 'settings')}${spaceTabButton('tasks', '学习安排', 'calendar')}${spaceTabButton('assistant', 'AI 助手', 'spark')}${spaceTabButton('assessment', '学习测评', 'check')}</nav><div class="space-tab-content">${body}</div>`;
}
function openSpaceTab(tab, spaceId = currentSpace()?.id) {
  if (!spaceId || !byId(spaceId)) return newSpaceModal();
  state.selected = spaceId; state.spaceTab = tab; state.spaceTabSpace = spaceId;
  if (tab === 'assessment') state.learningSection = 'diagnostic';
  render(); void loadRoute();
}
function legacyMaterialRedirect() {
  const material = graphMaterial() || data().materials.find(item => item.status !== 'archived');
  state.graphSpace = '';
  state.graph = null;
  state.graphUiKey = '';
  if (material) {
    state.graphMaterial = material.id;
    const target = '#/materials/graph/' + encodeURIComponent(material.id);
    if (location.hash !== target) location.hash = target;
  } else if (location.hash !== '#/materials') location.hash = '#/materials';
  return true;
}
function legacySpaceRedirect(current) {
  const tabs = { plan: 'tasks', assistant: 'assistant', learning: 'assessment' };
  const tab = tabs[current.name];
  if (!tab) return false;
  const space = currentSpace();
  if (!space) { if (location.hash !== '#/spaces') location.hash = '#/spaces'; return true; }
  state.selected = space.id; state.spaceTab = tab; state.spaceTabSpace = space.id;
  if (current.name === 'learning') state.learningSection = current.id || 'diagnostic';
  const target = '#/space/' + encodeURIComponent(space.id);
  if (location.hash !== target) location.hash = target;
  return true;
}
function render() {
  learning.sync(); renderVersion++; header();
  const current = route();
  const legacyGraph = current.name === 'graph' && !current.id;
  const legacySpace = ['plan', 'assistant', 'learning'].includes(current.name);
  if (legacyGraph) legacyMaterialRedirect();
  else if (legacySpace) legacySpaceRedirect(current);
  const previousWorkspace = $('.workspace-page');
  const savedForm = previousWorkspace?.querySelector('[data-workspace-form]');
  const savedFilter = previousWorkspace?.querySelector('[data-workspace-filter]');
  const previousKey = previousWorkspace?.dataset.workspaceKey;
  const views = { overview, spaces: spacesPage, materials: materialsPage, 'material-graph': () => materialsPage(), graph: graphPage, plan: planPage, assistant: assistantPage, space: () => spacePage(current.id),
    manage: () => renderWorkspace(workspace.snapshot()),
    learning: () => renderLearning({ mode: state.mode, space: currentSpace(), state: learning.snapshot(), spaceSelectHtml: spaceSelect(), section: current.id || 'diagnostic' }) };
  let content;
  if (state.loading) content = pageHeading('正在连接你的知识', '读取学习空间和资料，请稍候。') + '<div class="skeleton" role="status" aria-label="正在加载数据"></div>';
  else if (state.error && state.mode === 'live' && !(current.name === 'manage' && current.id === 'health')) content = pageHeading('让连接，重新发生。', '检查本地服务，然后继续你的学习。') + notice(esc(state.error), 'error') + empty('暂时无法读取学习数据', '请检查后端启动情况与本地会话令牌。你的真实数据仍保留在后端。', button('连接设置', 'settings', 'button button-primary', 'connection') + ' ' + button('重试', 'refresh', 'button button-secondary', 'refresh'), 'connection');
  else content = legacyGraph ? graphPage() : legacySpace ? spacePage(currentSpace()?.id) : (views[current.name] || (() => empty('这个页面还不存在', '回到概览，继续你的探索。', link('返回概览', '#/overview', 'button button-primary'))))();
  main.innerHTML = `<div class="container">${subbar()}${content}</div>`;
  if (current.name === 'manage') {
    const nextWorkspace = $('.workspace-page');
    if (nextWorkspace?.dataset.workspaceKey === previousKey) {
      if (savedForm) nextWorkspace.querySelector('[data-workspace-form]')?.replaceWith(savedForm);
      if (savedFilter) nextWorkspace.querySelector('[data-workspace-filter]')?.replaceWith(savedFilter);
    }
    updateWorkspaceConditions(main, workspace.snapshot());
  }
  document.title = `${current.name === 'manage' ? workspaceTitles[workspaceRoute(current.id).page] || '学习空间' : current.name === 'material-graph' ? '资料库 · 知识结构' : current.name === 'overview' ? '让每一步学习，都有方向' : routes.find(r => r[0] === current.name)?.[1] || (current.name === 'assistant' ? '学习助手' : '学习空间')} · KnowPath`;

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
  if (state.mode !== 'live') {
    if (state.mode === 'demo' && route().name === 'space' && state.spaceTab === 'assessment' && !learning.snapshot().knowledge && !state.loading && !state.error) await learning.loadKnowledge();
    return;
  }
  if (state.loading || (state.error && !(route().name === 'manage' && route().id === 'health'))) return;
  const current = route(), space = currentSpace();
  if (current.name === 'manage') { await workspace.load(); return; }
  if (['graph', 'material-graph'].includes(current.name)) {
    const context = graphContext();
    if (!context) return;
    let material = context.type === 'material' ? context.material : null;
    if (context.type === 'material' && state.graphMaterial !== context.id) state.graphMaterial = context.id;
    if (context.type === 'space' && state.graphSpace !== context.id) state.graphSpace = context.id;
    if (state.mode === 'live' && material && material.status !== 'ready') {
      try {
        const detail = await api.material(material.id);
        if (detail.material) Object.assign(material, detail.material);
      } catch { /* The graph request below provides the visible error. */ }
    }
    const key = state.mode + ':' + context.type + ':' + context.id + ':' + (material?.current_version_id || material?.version || context.space?.space_version || '');
    if (state.graphUiKey === key && state.graph) return;
    state.graphLoading = true; state.graphError = ''; state.graph = null; render();
    try {
      const result = state.mode === 'demo'
        ? context.type === 'material' ? demoMaterialGraph(material) : { nodes: context.space.nodes || [], edges: context.space.edges || [] }
        : context.type === 'material' ? await api.materialGraph(material) : await api.graph(context.space);
      if (state.mode === 'live' && graphContext()?.id === context.id) state.graph = result;
      if (context.type === 'material' && state.mode === 'live' && !result.nodes.length && ['uploaded', 'processing'].includes(material.status)) {
        setTimeout(() => { if (['graph', 'material-graph'].includes(route().name) && graphContext()?.id === material.id) { state.graphUiKey = ''; void loadRoute(); } }, 3000);
      }
    } catch (error) {
      if (graphContext()?.id === context.id) {
        const pending = Boolean(material && ['uploaded', 'processing'].includes(material.status));
        state.graphError = pending ? '资料正在解析，知识图谱生成后会自动刷新。' : error.message;
        if (pending) setTimeout(() => { if (['graph', 'material-graph'].includes(route().name) && graphContext()?.id === material.id) { state.graphUiKey = ''; void loadRoute(); } }, 3000);
      }
    }
    finally { state.graphLoading = false; if (['graph', 'material-graph'].includes(route().name)) render(); }
    return;
  }
  if (!space) return;
  if (current.name === 'learning' || current.name === 'space' && state.spaceTab === 'assessment') {
    if (!learning.snapshot().knowledge) await learning.loadKnowledge();
    if (!((route().name === 'learning' || route().name === 'space' && state.spaceTab === 'assessment')) || currentSpace()?.id !== space.id) return;
    if ((current.name === 'learning' && (!current.id || current.id === 'diagnostic') || current.name === 'space') && learning.snapshot().assessmentId
        && !learning.snapshot().diagnostic && !['failed', 'cancelled', 'completed', 'stale'].includes(learning.snapshot().assessmentStatus)) await learning.resume();
    const section = current.name === 'learning' ? current.id : state.learningSection;
    if (section === 'evolution' && !learning.snapshot().evolution) await learning.loadEvolution();
    if (section === 'replay' && !learning.snapshot().replay) await learning.replay();
  }
  if (current.name === 'space' && !state.topics.has(space.id)) {
    try { const topics = await api.topics(space); if (state.mode === 'live' && byId(space.id)) { state.topics.set(space.id, topics); if (route().name === 'space') render(); } }
    catch (error) { toast(error.message, true); }
  }
  if ((current.name === 'plan' || current.name === 'space' && state.spaceTab === 'tasks') && !state.plans.has(space.id)) {
    const planId = storageRead(session, 'knowpath-plan-ids', {})[space.id];
    if (planId) {
      state.planLoading = true; state.planError = ''; render();
      try { const plan = await api.plan(planId); if (state.mode === 'live' && byId(space.id)) state.plans.set(space.id, { ...plan, space_id: space.id }); }
      catch (error) { state.planError = error.message; }
      finally { state.planLoading = false; if (route().name === 'plan' || route().name === 'space' && state.spaceTab === 'tasks') render(); }
    }
  }
}

function settingsModal() {
  openModal('连接你的学习空间', `<p class="modal-intro">示例体验随时可用。连接本地服务后，即可访问你自己的资料与学习记录。</p><form id="connection-form"><div class="form-field"><label for="service-address">本地服务地址</label><input id="service-address" value="http://127.0.0.1:8000" readonly><small>与项目中的 Python 后端保持一致。</small></div><div class="form-field"><label for="local-token">本地会话令牌</label><input id="local-token" name="token" type="password" autocomplete="off" spellcheck="false" placeholder="输入本地服务令牌" value="${esc(token)}"><small>只保存在当前标签页，不会写入前端代码。</small></div><div class="modal-note">在项目的 py 目录启动后端服务，然后填入本地令牌。详细命令见 frontend/README.md。</div><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('使用示例体验', 'demo-mode', 'button button-secondary')}<button class="button button-primary" type="submit">${icon('connection')}连接本地服务</button></div></form><div class="connection-state" role="status"></div>${state.mode === 'live' ? manageLink('查看服务状态', 'health', null) : ''}`);
}
function newSpaceModal() {
  const choices = data().materials.filter(m => m.status === 'ready');
  openModal('给学习，一个新空间', `<p class="modal-intro">先选资料和学习目标，创建后再在空间内选择范围并生成学习任务。</p>${!choices.length ? notice('需要至少一份已就绪的资料。真实模式下，资料还需完成图谱审核发布。', 'warning') : ''}<form id="new-space-form"><div class="form-field"><label for="space-name">空间名称</label><input id="space-name" name="name" maxlength="100" placeholder="例如：从零理解线性代数" required></div><div class="form-field"><label for="space-goal">你想达到什么目标？</label><textarea id="space-goal" name="goal" maxlength="1000" rows="2" placeholder="例如：能够直观理解矩阵与线性变换"></textarea></div><fieldset class="form-field"><legend>选择学习资料（1—5 份）</legend><div class="material-choices">${choices.map(m => `<label class="check-label"><input type="checkbox" name="material_ids" value="${esc(m.id)}">${esc(m.name)}</label>`).join('') || '<p class="subtle">先在资料库导入第一份资料。</p>'}</div></fieldset><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button class="button button-primary" type="submit" ${!choices.length ? 'disabled' : ''}>创建学习空间</button></div></form>${!choices.length ? link('前往资料库', '#/materials') : ''}`);
}
async function createSpace(form) {
  const values = new FormData(form);
  const ids = values.getAll('material_ids');
  const name = values.get('name').trim(), goal = values.get('goal').trim();
  if (!name) throw new Error('请给学习空间起一个名字。');
  if (ids.length < 1 || ids.length > 5) throw new Error('请选择 1—5 份学习资料。');
  const payload = { name, material_ids: ids, ...(goal ? { goal } : {}) };
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
  state.selected = space.id; state.spaceTab = 'scope'; state.spaceTabSpace = space.id; closeModal(); go('space', space.id); toast('学习空间已创建，请先选择学习范围，再生成学习任务。');
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
      const pdfHash = /\.pdf$/i.test(file.name) ? await validatePdf(file) : '';
      if (mode === 'demo') {
        const isPdf = /\.pdf$/i.test(file.name);
        const text = isPdf ? '' : await file.text();
        if (text.length > 1000000) throw new Error('示例模式仅保存 1 MB 以内的文本内容，大文件请连接后端导入。');
        const candidate = { id: `demo-material-${uid()}`, name: file.name, type: file.name.split('.').pop().toLowerCase(), size_bytes: file.size, text, status: isPdf ? 'uploaded' : 'ready', created_at: new Date().toISOString(), version: 1 };
        const totalText = demo.materials.reduce((sum, m) => sum + (m.text?.length || 0), 0);
        if (totalText + text.length > 1800000) throw new Error('示例资料容量已满，请连接后端管理更多资料。');
        demo.materials.unshift(candidate); saveDemo();
        if (isPdf) {
          const saved = await pdfLibrary.save({ key: pdfKey('demo', candidate.id, candidate.id), file, hash: pdfHash, verified: true });
          if (!saved) toast('PDF 本次可以打开，但浏览器未能永久保存；下次需重新选择。', true);
        }
      } else {
        const uploaded = await api.upload(file); state.live.materials.unshift(uploaded.material);
        if (pdfHash) {
          const saved = await pdfLibrary.save({ key: pdfKey('live', uploaded.material.id, uploaded.version.id), file, hash: pdfHash, verified: true });
          if (!saved) toast('资料已上传；PDF 本次可打开，但浏览器未能永久保存，下次需重新选择。', true);
        }
      }
      completed++;
    } catch (error) { errors.push(`${file.name}：${error.message}`); }
    const progress = $('#upload-progress'); if (progress) progress.textContent = `已完成 ${completed} / ${selected.length} 份资料`;
  }
  if (errors.length) openModal('资料导入结果', `<p class="modal-intro">成功导入 ${completed} 份资料。以下文件需要处理后重试：</p>${errors.map(error => notice(esc(error), 'error')).join('')}<div class="form-actions">${button('知道了', 'close-modal', 'button button-primary')}</div>`);
  else { closeModal(); toast(mode === 'demo' ? '资料已保存，正在整理知识图谱。' : '资料已上传，正在生成知识图谱。'); }
  state.materialFilter = 'all'; state.query = '';
  go('materials');
}
async function viewMaterial(materialId) {
  const material = data().materials.find(m => m.id === materialId);
  if (!material) throw new Error('资料不存在，请刷新后重试。');
  return openSourcePage({ material_id: material.id, material_version_id: material.current_version_id }, { materialView: true });
}
async function reviewMaterial(materialId) {
  if (state.mode === 'live') { closeModal(); location.hash = workspacePath('material-graph', materialId); return; }
  openModal('审核知识图谱', '<div class="loading-block" role="status">正在读取候选知识结构…</div>');
  const version = modalVersion, diff = await api.graphDiff(materialId);
  if (version !== modalVersion || !modal.open) return;
  const changes = [...(diff.added || []).map(t => ({ label: '新增', item: t })), ...(diff.changed || []).map(t => ({ label: '修改', item: t })), ...(diff.removed || []).map(t => ({ label: '移除', item: t }))];
  state.review = { materialId, diff };
  const hasConflicts = Boolean(diff.conflicts?.length);
  openModal('审核知识图谱', `<p class="modal-intro">确认资料中的主题变化后，发布一个新的知识快照。已有学习空间不会自动切换资料版本。</p>${!diff.candidate_revision_id ? notice(diff.base_graph_version ? '当前图谱已发布，没有待审核的更新。可以用这份资料创建学习空间。' : '尚未生成候选图谱，请等待解析完成后刷新。') : `<div class="source-content">${changes.map(({ label, item }) => `${label}：${esc(item.title || item.after?.title || item.name || item.topic_id || item.id || '主题变更')}`).join('\n') || '没有主题差异。'}</div>${hasConflicts ? notice(`存在 ${diff.conflicts.length} 项知识冲突，需要通过后端审核工具解决后再发布。此页面不会自动替你选择冲突结论。`, 'warning') : ''}${diff.status !== 'pending_review' ? notice('图谱索引仍在准备中，请稍后重新打开审核。', 'warning') : ''}`}<div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('关闭', 'close-modal', 'button button-secondary')}${diff.candidate_revision_id ? button('确认并发布', 'publish-graph', 'button button-primary', 'check', hasConflicts || diff.status !== 'pending_review' ? 'disabled' : '') : ''}</div>`);
}
async function scopeModal({ returnToPlan = false } = {}) {
  const space = currentSpace(); if (!space) return newSpaceModal();
  openModal('选择这次的学习范围', '<div class="loading-block" role="status">正在读取知识主题…</div>');
  const version = modalVersion;
  const topics = state.mode === 'demo' ? space.nodes : await api.topics(space);
  if (version !== modalVersion || !modal.open) return;
  state.topics.set(space.id, topics);
  openModal('选择这次的学习范围', `<p class="modal-intro">${returnToPlan ? '先选定至少一个知识主题，保存后会自动回到学习任务设置。' : '先专注于真正想弄懂的内容。你可以暂时排除前置主题，并决定是否自动纳入必要前置知识。'}</p><form id="scope-form" data-space="${esc(space.id)}" data-return-plan="${returnToPlan}"><div class="scope-picker-grid"><fieldset class="form-field scope-picker"><legend>纳入学习</legend><div class="material-choices" style="max-height:280px">${topics.map(t => `<label class="check-label"><input type="checkbox" name="topic_ids" value="${esc(t.id)}" ${space.topic_ids?.includes(t.id) ? 'checked' : ''}>${esc(t.title || t.name)}</label>`).join('') || notice('没有可选择的主题，请先检查资料的解析与发布状态。')}</div></fieldset><fieldset class="form-field scope-picker"><legend>暂时排除</legend><div class="material-choices" style="max-height:280px">${topics.map(t => `<label class="check-label"><input type="checkbox" name="excluded_topic_ids" value="${esc(t.id)}" ${space.excluded_topic_ids?.includes(t.id) ? 'checked' : ''}>${esc(t.title || t.name)}</label>`).join('') || '<p class="subtle">没有可排除的主题。</p>'}</div></fieldset></div><label class="check-label scope-prerequisite"><input type="checkbox" name="prerequisites" ${space.include_prerequisites !== false ? 'checked' : ''}>自动纳入必要的前置知识</label><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button type="submit" class="button button-primary" ${topics.length ? '' : 'disabled'}>保存学习范围</button></div></form>`);
}
function planSetting(form, name, customName, min, max, label) {
  const select = form.elements[name];
  const custom = form.elements[customName];
  const customField = form.querySelector(`[data-plan-custom="${name}"]`);
  const isCustom = select?.value === 'custom';
  if (customField) customField.hidden = !isCustom;
  if (custom) custom.required = isCustom;
  const raw = isCustom ? custom?.value : select?.value;
  const value = Number(raw);
  const valid = Number.isInteger(value) && value >= min && value <= max;
  return { value, valid, isCustom, message: `${label}需填写 ${min}—${max} 之间的整数。` };
}
function planSettings(form) {
  const sessions = planSetting(form, 'session_count', 'session_count_custom', 3, 5, '学习次数');
  const minutes = planSetting(form, 'minutes_per_session', 'minutes_per_session_custom', 10, 120, '单次时长');
  return { sessions, minutes };
}
function updatePlanPreview(form) {
  if (!form) return;
  const space = byId(form.dataset.space);
  const { sessions, minutes } = planSettings(form);
  const preview = $('#plan-preview', form);
  const submit = $('button[type="submit"]', form);
  if (!sessions.valid || !minutes.valid) {
    if (preview) {
      preview.className = 'plan-preview over';
      preview.innerHTML = `<strong>请完善自定义设置</strong><span>${!sessions.valid ? sessions.message : minutes.message}</span>`;
    }
    if (submit) submit.disabled = true;
    return;
  }
  const total = sessions.value * minutes.value;
  if (preview) {
    preview.className = 'plan-preview';
    preview.innerHTML = `<strong>本次会安排最多 ${sessions.value} 次</strong><span>预计学习 ${total} 分钟 · 每次约 ${minutes.value} 分钟。实际任务会按知识主题和掌握状态生成。</span>`;
  }
  if (submit) submit.disabled = false;
}
function planModal() {
  const space = currentSpace(); if (!space) return newSpaceModal();
  if (!space.topic_ids?.length) { return scopeModal({ returnToPlan: true }); }
  openModal('生成学习任务', `<p class="modal-intro">为「${esc(space.name)}」的 ${Number(space.topic_ids.length)} 个学习主题生成一组可执行任务。</p><form id="plan-form" data-space="${esc(space.id)}"><div class="form-field"><label for="session-count">最多安排几次学习？</label><select id="session-count" name="session_count"><option value="3">3 次</option><option value="4">4 次</option><option value="5">5 次</option><option value="custom">自定义</option></select></div><div class="form-field plan-custom-field" data-plan-custom="session_count" hidden><label for="session-count-custom">自定义学习次数</label><input id="session-count-custom" name="session_count_custom" type="number" min="3" max="5" step="1" placeholder="请输入 3—5 次"><small>可设置 3—5 次学习。</small></div><div class="form-field"><label for="session-minutes">每次最多学习多久？</label><select id="session-minutes" name="minutes_per_session"><option value="15">15 分钟，轻松起步</option><option value="25" selected>25 分钟，保持专注</option><option value="45">45 分钟，深入理解</option><option value="custom">自定义</option></select></div><div class="form-field plan-custom-field" data-plan-custom="minutes_per_session" hidden><label for="session-minutes-custom">自定义单次时长（分钟）</label><input id="session-minutes-custom" name="minutes_per_session_custom" type="number" min="10" max="120" step="1" placeholder="请输入 10—120 分钟"><small>可设置 10—120 分钟。</small></div><label class="check-label"><input type="checkbox" name="include_review" checked>有到期内容时安排复习</label><div id="plan-preview" class="plan-preview" aria-live="polite"></div><div class="form-error" data-form-error hidden role="alert"></div><div class="form-actions">${button('取消', 'close-modal', 'button button-secondary')}<button type="submit" class="button button-primary">生成学习任务</button></div></form>`);
  updatePlanPreview($('#plan-form'));
}
async function createPlan(form) {
  const space = byId(form.dataset.space), values = new FormData(form);
  if (!space) throw new Error('学习空间已变化，请返回空间后重试。');
  state.planError = '';
  const { sessions, minutes } = planSettings(form);
  if (!sessions.valid) throw new Error(sessions.message);
  if (!minutes.valid) throw new Error(minutes.message);
  const payload = { session_count: sessions.value, minutes_per_session: minutes.value, include_review: values.has('include_review'), rebuild_mode: 'initial' };
  const status = $('#plan-preview', form);
  if (status) { status.className = 'plan-preview pending'; status.innerHTML = '<strong>正在生成学习任务…</strong><span>正在读取知识状态，请稍候，不要重复点击。</span>'; }
  form.querySelectorAll('select, input, button').forEach(control => { control.disabled = true; });
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
    if (!result?.plan_id) throw new Error('服务未返回学习安排标识，请刷新后重试。');
    if (result.run_id && !['ready', 'succeeded', 'completed'].includes(result.status)) await api.waitRun(result.run_id);
    const plans = storageRead(session, 'knowpath-plan-ids', {}); plans[space.id] = result.plan_id; storageWrite(session, 'knowpath-plan-ids', plans);
    const plan = await api.plan(result.plan_id); state.plans.set(space.id, { ...plan, space_id: space.id });
  }
  state.timerDuration = payload.minutes_per_session; resetTimer(); closeModal(); openSpaceTab('tasks', space.id); toast('学习任务已就绪，选择一个任务开始学习。');
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
  openModal('从好奇，到理解', `<div class="help-content"><h3>1. 先把资料放进来</h3><p>在资料库导入 PDF、Markdown 或 TXT。资料会自动进入解析流程，完成后可在学习空间里浏览知识结构。</p><h3>2. 为目标创建一个空间</h3><p>填写空间名称和学习目标，选择 1—5 份资料。每周时间和目标日期不再是创建前置条件。</p><h3>3. 在空间里定范围</h3><p>进入学习空间的“学习范围”，选择本次要学的主题，暂时排除不需要的内容，并决定是否自动纳入前置知识。</p><h3>4. 生成任务并学习</h3><p>在“学习安排”里设置学习次数、单次时长和复习选项。任务会根据当前范围生成；可以直接打开资料、记录学习会话和请求讲解。</p><h3>5. 继续提问和测评</h3><p>“AI 助手”只围绕当前空间资料回答并提供引用；“学习测评”按当前范围出题，结果和复核入口也从空间进入。</p><h3>用键盘也很顺手</h3><p><code>Ctrl / ⌘ + K</code> 搜索；<code>Esc</code> 关闭弹窗；<code>Ctrl / ⌘ + Enter</code> 发送问题。Tab 可在导航、按钮和知识主题之间移动。</p></div><div class="form-actions">${button('开始探索', 'close-modal', 'button button-primary')}</div>`);
}
function searchModal() {
  openModal('找到你的下一步', `<div class="command-input">${icon('search')}<input data-input="global-search" id="global-search" aria-label="搜索全部学习内容" placeholder="搜索空间、资料，或前往一个页面" autocomplete="off"><kbd>Esc</kbd></div><div id="global-results">${searchResults('')}</div>`, 'search-dialog');
}
function searchResults(query) {
  const text = query.trim().toLowerCase();
  const groups = [
    ['前往', routes.filter(([, label]) => label.includes(text)).map(([name, label]) => `<a class="search-result" href="#/${name}">${icon(name === 'graph' ? 'graph' : name === 'plan' ? 'calendar' : 'arrow')}<span>${label}</span><small>页面</small></a>`)],
    ['学习空间', data().spaces.filter(s => s.name.toLowerCase().includes(text)).slice(0, 7).map(s => `<a class="search-result" href="#/space/${encodeURIComponent(s.id)}">${icon('layers')}<span>${esc(s.name)}</span><small>学习空间</small></a>`)],
    ['学习资料', data().materials.filter(m => m.name.toLowerCase().includes(text)).slice(0, 7).map(m => `<button class="search-result" data-action="view-material" data-id="${esc(m.id)}">${icon('file')}<span>${esc(m.name)}</span><small>${normalizeType(m).toUpperCase()}</small></button>`)],
  ];
  return groups.some(([, results]) => results.length) ? groups.filter(([, results]) => results.length).map(([title, results]) => `<div class="search-group-title">${title}</div>${results.join('')}`).join('') : '<p class="modal-intro" style="padding-top:24px">没有找到相关内容。试试更短的关键词，例如“矩阵”。</p>';
}
function updateChat() { if (route().name !== 'assistant' && !(route().name === 'space' && state.spaceTab === 'assistant')) return; const question = $('#question')?.value || ''; render(); if ($('#question')) $('#question').value = question; }
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
        const job = await api.sendMessage(space.id, question, state.chats.get(space.id), state.chatMaterials.get(space.id) || []);
        state.pendingJob = { ...job, spaceId: space.id }; state.chats.set(space.id, job.session_id);
        if (controller.signal.aborted) { await api.cancelRun(job.run_id); state.pendingJob = null; throw new DOMException('操作已停止', 'AbortError'); }
      }
      result = await api.readMessage(state.pendingJob, { signal: controller.signal, onStatus: text => { const element = $('#chat-status'); if (element) { element.hidden = false; element.textContent = text; } } });
    }
    if (!byId(space.id)) return;
    list.push({ role: 'assistant', text: result.text, citations: result.citations || [] });
    state.messages.set(space.id, list); state.pendingJob = null;
  } catch (error) {
    if (error.name !== 'AbortError') { toast(error.message, true); if (state.pendingJob && error.code !== 'RUN_PENDING' && error.status >= 400) state.pendingJob = null; }
  } finally { state.sending = false; state.chatController = null; updateChat(); }
}
function materialSpace(materialId, versionId) {
  const matches = space => (space?.bindings || []).some(binding => binding.material_id === materialId && (!versionId || binding.material_version_id === versionId));
  return matches(currentSpace()) ? currentSpace() : data().spaces.find(matches);
}
function reservePdfWindow() {
  const target = window.open('about:blank', '_blank');
  if (target) {
    target.opener = null;
    target.document.title = '正在打开 PDF…';
    target.document.body.textContent = '正在定位知识来源，请稍候…';
  }
  return target;
}
function sourceIsCurrent(context) {
  return sourceReader === context && context.modalVersion === modalVersion && modal.open && state.mode === context.mode;
}
function sourceModal(context, content) {
  openModal('资料来源', content, 'source-dialog');
  context.modalVersion = modalVersion;
}
function renderSourceReader(context, message = '') {
  const page = sourcePage(context.source), linked = context.pdf;
  const isPdf = normalizeType(context.material) === 'pdf';
  const fileLabel = linked ? '更换 PDF' : isPdf ? '选择原 PDF' : '关联阅读用 PDF';
  const intro = linked
    ? `${esc(linked.name)}${linked.verified && page ? ' · 第 ' + page + ' 页' : ' · 未提供可定位页码'}`
    : isPdf ? '首次阅读请补选当时导入的 PDF。保存后，点击来源即可直接打开。'
      : '当前来源是文字资料，可直接阅读下方原文；也可以关联对应 PDF 方便阅读。';
  sourceModal(context, `<h3 class="source-document-name">${esc(context.material.name)}</h3>
    <p class="modal-intro">${intro}</p>
    ${message ? notice(esc(message), 'warning') : ''}
    <div class="source-reader-actions">${linked ? button('打开 PDF' + (linked.verified && page ? ' · 第 ' + page + ' 页' : ''), 'open-source-pdf', 'button button-primary', 'file') : ''}
      ${context.allowUpload === false ? '' : `<label class="button button-secondary file-picker">${icon('file')}${fileLabel}<input type="file" id="source-pdf-input" accept=".pdf,application/pdf" aria-label="${fileLabel}"></label>`}
      ${context.materialView && context.mode === 'live' ? button('审核知识图谱', 'review-material', 'button button-soft', 'graph', 'data-id="' + esc(context.material.id) + '"') : ''}
    </div>
    <p class="source-local-note">${context.allowUpload === false ? '浏览器阻止了新标签页，请允许弹窗后重新点击“查看来源”。' : `PDF 仅保存在当前浏览器，关闭后仍可使用。清理网站数据或换浏览器后需重新选择。${!isPdf ? '关联 PDF 不会改变知识来源；没有准确页码时从文档首页打开。' : ''}`}</p>
    <div class="form-error" data-form-error hidden role="alert"></div>
    ${context.source.text ? `<div class="source-excerpt-heading"><h3>来源原文</h3>${page ? '<span>第 ' + page + ' 页</span>' : ''}</div><div class="source-content source-reader-text">${esc(context.source.text)}</div>` : notice('当前来源没有文字片段，可选择 PDF 阅读。')}
    <p class="source-ref">${esc((context.source.section_path || []).join(' / '))}</p>`);
}
function openPdfRecord(context, target) {
  const url = URL.createObjectURL(context.pdf.blob);
  pdfUrls.add(url);
  const destination = pdfLocation(url, context.source, context.pdf.verified);
  if (target?.closed) { URL.revokeObjectURL(url); pdfUrls.delete(url); return false; }
  if (target) target.location.replace(destination);
  else {
    target = window.open(destination, '_blank');
    if (target) target.opener = null;
  }
  if (!target) {
    URL.revokeObjectURL(url); pdfUrls.delete(url);
    renderSourceReader(context, '浏览器阻止了自动打开，请点击“打开 PDF”继续。');
    return false;
  }
  // Retain each Blob while its PDF tab is open, including after the dialog closes.
  const timer = setInterval(() => {
    if (target.closed) { clearInterval(timer); URL.revokeObjectURL(url); pdfUrls.delete(url); }
  }, 2000);
  return true;
}
function sourceDocumentHtml(context, message = '') {
  const materialName = context.material?.name || '资料来源';
  const source = context.source || {};
  const page = sourcePage(source);
  const section = (source.section_path || []).filter(Boolean).join(' / ');
  const text = source.text || '当前资料还没有可展示的解析正文。';
  const label = message ? `<div class="notice error">${esc(message)}</div>` : '';
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(materialName)} · 资料来源</title><style>
    :root{color-scheme:light;--ink:#1e293b;--muted:#64748b;--line:#e2e8f0;--surface:#fff;--soft:#f8fafc;--accent:#2563eb;}
    *{box-sizing:border-box}body{margin:0;background:#f1f5f9;color:var(--ink);font:16px/1.8 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
    main{width:min(920px,calc(100% - 40px));margin:40px auto 64px;background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:0 12px 36px #0f172a12;padding:42px 54px}
    .eyebrow{color:var(--accent);font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}.title{margin:8px 0 4px;font-size:30px;line-height:1.3}.meta{color:var(--muted);font-size:14px;margin:0 0 30px}.content{white-space:pre-wrap;overflow-wrap:anywhere;font-size:17px;line-height:2}.ref{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}.notice{padding:12px 14px;margin:0 0 22px;border-radius:10px;background:#eff6ff;color:#1d4ed8;font-size:14px}.notice.error{background:#fff7ed;color:#c2410c}
    @media print{body{background:#fff}main{box-shadow:none;border:0;margin:0;width:100%;padding:0}}
  </style></head><body><main><div class="eyebrow">KnowPath · 资料来源</div><h1 class="title">${esc(materialName)}</h1><p class="meta">${page ? `第 ${page} 页` : '原文阅读'}${section ? ` · ${esc(section)}` : ''}</p>${label}<article class="content">${esc(text)}</article><p class="ref">此页面由当前学习资料的解析内容生成，无需重新上传 PDF。${context.materialView ? '返回原页面可继续审核知识图谱。' : ''}</p></main></body></html>`;
}
function openTextRecord(context, target, message = '') {
  const url = URL.createObjectURL(new Blob([sourceDocumentHtml(context, message)], { type: 'text/html;charset=utf-8' }));
  pdfUrls.add(url);
  if (target?.closed) { URL.revokeObjectURL(url); pdfUrls.delete(url); return false; }
  if (target) target.location.replace(url);
  else {
    target = window.open(url, '_blank');
    if (target) target.opener = null;
  }
  if (!target) { URL.revokeObjectURL(url); pdfUrls.delete(url); return false; }
  const timer = setInterval(() => {
    if (target.closed) { clearInterval(timer); URL.revokeObjectURL(url); pdfUrls.delete(url); }
  }, 2000);
  return true;
}
async function loadSourceForPage(context) {
  if (context.mode === 'demo') {
    context.source = { text: context.material.text || '', section_path: [] };
    return;
  }
  if (context.versionId && context.ref.chunk_id) {
    context.source = context.spaceId ? await api.source(context.ref, context.spaceId) : await api.materialSource(context.ref);
    return;
  }
  const result = await api.material(context.material.id, context.spaceId);
  if (context.versionId && result.version?.id !== context.versionId) throw new Error('来源版本与当前资料不同，请刷新资料后重试。');
  context.versionId = result.version?.id || context.versionId;
  context.key = pdfKey(context.mode, context.material.id, context.versionId);
  context.material = result.material || context.material;
  const chunks = result.version?.chunks || [];
  context.source = {
    text: chunks.map(chunk => chunk.text || '').filter(Boolean).join('\n\n'),
    section_path: chunks[0]?.section_path || [],
    page: chunks.find(chunk => sourcePage(chunk))?.page || null,
  };
}
async function openSourcePage(ref, { materialView = false } = {}) {
  if (!ref?.material_id) throw new Error('这条知识没有有效的来源，请重新加载图谱。');
  const material = data().materials.find(item => item.id === ref.material_id) || { id: ref.material_id, name: '原始资料', type: 'txt' };
  const space = materialView ? materialSpace(material.id, ref.material_version_id) : currentSpace();
  const versionId = ref.material_version_id || (space?.bindings || []).find(binding => binding.material_id === material.id)?.material_version_id || material.current_version_id;
  const context = { ref: { ...ref, material_version_id: versionId }, material, materialView, mode: state.mode, spaceId: space?.id, versionId, source: {}, pdf: null, allowUpload: false };
  context.key = pdfKey(context.mode, material.id, context.mode === 'demo' ? material.id : versionId);
  // Reserve the tab synchronously, while the browser still considers this a user gesture.
  const target = reservePdfWindow();
  if (!target) {
    return showSource(ref, { materialView, forceReader: true, allowUpload: false });
  }
  sourceReader = context;
  try {
    await loadSourceForPage(context);
    context.pdf = await pdfLibrary.get(context.key);
    if (context.pdf && openPdfRecord(context, target)) {
      toast('已在新标签页打开资料来源。');
      return;
    }
    if (!openTextRecord(context, target)) throw new Error('浏览器无法打开资料来源页面，请允许弹窗后重试。');
    toast('已在新标签页打开资料来源原文。');
  } catch (error) {
    if (!target.closed) openTextRecord(context, target, error.message || '资料来源暂时无法打开。');
    else toast(error.message || '资料来源暂时无法打开。', true);
  }
}
async function showSource(ref, { materialView = false, forceReader = false, allowUpload = true } = {}) {
  if (!ref?.material_id) throw new Error('这条知识没有有效的来源，请重新加载图谱。');
  const material = data().materials.find(item => item.id === ref.material_id) || { id: ref.material_id, name: '原始资料', type: 'txt' };
  const space = materialView ? materialSpace(material.id, ref.material_version_id) : currentSpace();
  const versionId = ref.material_version_id || (space?.bindings || []).find(binding => binding.material_id === material.id)?.material_version_id || material.current_version_id;
  const context = { ref: { ...ref, material_version_id: versionId }, material, materialView, mode: state.mode, spaceId: space?.id, versionId, source: {}, pdf: null, allowUpload };
  context.key = pdfKey(context.mode, material.id, context.mode === 'demo' ? material.id : versionId);
  const target = !forceReader && pdfLibrary.peek(context.key) ? reservePdfWindow() : null;
  sourceReader = context;
  sourceModal(context, '<div class="loading-block" role="status">正在定位资料来源…</div>');
  try {
    if (context.mode === 'demo') context.source = { text: material.text || '' };
    else if (versionId && ref.chunk_id) context.source = await api.source(context.ref, context.spaceId);
    else {
      const result = await api.material(material.id, context.spaceId);
      if (versionId && result.version?.id !== versionId) throw new Error('来源版本与当前资料不同，请重新加载图谱后打开具体知识来源。');
      context.versionId = result.version?.id;
      context.key = pdfKey(context.mode, material.id, context.versionId);
      context.material = result.material;
      context.source = { text: (result.version?.chunks || []).map(chunk => chunk.text).join('\n\n') };
    }
    if (!sourceIsCurrent(context)) { target?.close(); return; }
    context.pdf = await pdfLibrary.get(context.key);
    if (!sourceIsCurrent(context)) { target?.close(); return; }
    renderSourceReader(context);
    if (context.pdf && !forceReader && openPdfRecord(context, target)) {
      closeModal();
      toast('已打开 PDF，当前知识图谱已保留。');
    } else if (!context.pdf) target?.close();
  } catch (error) {
    target?.close();
    if (sourceIsCurrent(context)) sourceModal(context, `${notice(esc(error.message), 'error')}<div class="form-actions">${button('重试', 'retry-source', 'button button-primary')}${button('关闭', 'close-modal', 'button button-secondary')}</div>`);
  }
}
async function attachSourcePdf(file) {
  const context = sourceReader;
  if (!file || !context || !sourceIsCurrent(context)) return;
  const target = reservePdfWindow();
  const picker = $('#source-pdf-input'); if (picker) picker.disabled = true;
  try {
    let expectedHash = '';
    const isPdf = normalizeType(context.material) === 'pdf';
    if (context.mode === 'live') {
      if (!context.versionId) throw new Error('无法确定来源版本，请重新打开资料。');
      if (isPdf) {
        const versions = await api.materialVersions(context.material.id);
        expectedHash = versions.find(version => version.id === context.versionId)?.content_hash;
        if (!expectedHash) throw new Error('无法核对原 PDF 的资料版本，请稍后重试。');
      }
    }
    const hash = await validatePdf(file, expectedHash);
    if (!sourceIsCurrent(context)) { target?.close(); return; }
    const saved = await pdfLibrary.save({ key: context.key, file, hash, verified: isPdf });
    if (!sourceIsCurrent(context)) { target?.close(); return; }
    context.pdf = await pdfLibrary.get(context.key);
    renderSourceReader(context, saved ? '' : '浏览器未能永久保存 PDF，本次仍可阅读；下次需要重新选择。');
    if (openPdfRecord(context, target)) {
      closeModal();
      toast(saved ? 'PDF 已关联，以后点击来源即可直接打开。' : '已打开 PDF，但未能永久保存；下次需重新选择。', !saved);
    }
  } catch (error) {
    target?.close();
    if (sourceIsCurrent(context)) showError(error);
  } finally {
    if (sourceIsCurrent(context)) { const input = $('#source-pdf-input'); if (input) { input.disabled = false; input.value = ''; } }
  }
}
document.addEventListener('click', async event => {
  const workspaceAction = event.target.closest('[data-workspace-action]');
  if (workspaceAction) { await workspace.action(workspaceAction.dataset.workspaceAction, workspaceAction.dataset.value); return; }
  if (event.target.closest('.skip-link')) { event.preventDefault(); main.focus(); return; }
  const jump = event.target.closest('[data-learning-jump]');
  if (jump) {
    event.preventDefault();
    const section = document.getElementById(jump.dataset.learningJump);
    if (section) { section.scrollIntoView({ block: 'start' }); section.setAttribute('tabindex', '-1'); section.focus({ preventScroll: true }); }
    return;
  }
  const element = event.target.closest('[data-action]');
  if (!element) { if (event.target.closest('a[href^="#/"]') && modal.open) closeModal(); return; }
  const action = element.dataset.action, id = element.dataset.id;
  const actions = {
    'learning-knowledge': () => learning.loadKnowledge(), 'learning-resume': () => learning.resume(),
    'learning-finalize': () => learning.finalize(), 'learning-evolution': () => learning.loadEvolution(),
    'learning-replay': () => learning.replay(), 'learning-cancel': () => learning.cancelGeneration(),
    'close-modal': closeModal, search: searchModal, help: helpModal,
    settings: () => { settingsModal(); if (state.mode === 'demo') $('.modal-body', modal).insertAdjacentHTML('beforeend', '<div style="margin-top:23px">' + button('恢复初始示例', 'reset-demo', 'text-link') + '</div>'); },
    'reset-demo': () => openModal('恢复初始示例？', '<p class="modal-intro">将移除你在此浏览器中新增的示例空间、示例资料和任务修改，并恢复内置示例。真实后端数据不会改变。</p><div class="form-actions">' + button('取消', 'close-modal', 'button button-secondary') + button('确认恢复示例', 'confirm-reset-demo', 'button button-primary') + '</div>'),
    'confirm-reset-demo': () => { Object.assign(demo, makeDemo()); state.selected = demo.spaces[0].id; state.graphSelected = ''; state.query = ''; state.materialFilter = 'all'; state.spaceFilter = 'all'; state.messages.clear(); saveDemo(); closeModal(); go('overview'); toast('已恢复初始示例。'); },
    privacy: () => openModal('你的知识，安心放在这里', '<div class="help-content"><h3>示例体验</h3><p>示例空间、导入的文本内容和任务状态保存在当前浏览器的 localStorage。不会上传到任何服务。导入或关联的 PDF 保存在当前浏览器的 IndexedDB 中，用于直接打开原文件；清除网站数据会一并移除。</p><h3>本地服务</h3><p>真实资料与问答通过本地后端处理。前端只向 127.0.0.1:8000 发送请求；模型服务的调用由后端配置决定。前端不包含统计追踪、广告脚本或外部字体。</p><h3>连接凭据</h3><p>本地令牌只保存在当前标签页的 sessionStorage，并通过 X-Local-Token 请求头发送。聊天界面记录保留在本次页面会话中；后台记录遵循后端的持久化设置。</p></div>'),
    'mobile-menu': () => { const nav = $('#main-nav'); const open = nav.classList.toggle('open'); element.setAttribute('aria-expanded', String(open)); element.setAttribute('aria-label', open ? '收起导航' : '展开导航'); },
    'new-space': newSpaceModal, 'new-plan': planModal, 'edit-scope': scopeModal,
    'archive-space': () => spaceChangeModal(id, 'archive'), 'restore-space': () => spaceChangeModal(id, 'restore'), 'delete-space': () => spaceChangeModal(id, 'delete'),
    'continue-learning': () => currentSpace() ? openSpaceTab('tasks') : newSpaceModal(),
    'demo-mode': () => { state.chatController?.abort(); state.mode = 'demo'; state.error = ''; state.pendingJob = null; state.selected = demo.spaces[0]?.id || ''; state.messages.clear(); storageWrite(session, MODE_KEY, 'demo'); closeModal(); render(); toast('已切换到示例体验，所有操作仅影响本地示例。'); },
    refresh: () => { state.graphSpace = ''; state.graph = null; state.graphUiKey = ''; state.topics.clear(); state.plans.clear(); return refreshData(); },
    'space-filter': () => { state.spaceFilter = id; render(); },
    'material-filter': () => { state.materialFilter = id; render(); },
    upload: () => $('#file-input')?.click(),
    'view-material': () => viewMaterial(id), 'review-material': () => reviewMaterial(id),
    'space-tab': () => openSpaceTab(element.dataset.tab || 'overview', currentSpace()?.id),
    'learning-section': () => { event.preventDefault(); state.spaceTab = 'assessment'; state.learningSection = element.dataset.section || 'diagnostic'; render(); void loadRoute(); },
    'material-graph': () => { state.graphMaterial = id; state.graphSpace = ''; state.graph = null; state.graphUiKey = ''; state.graphSelected = ''; state.graphHistory = []; location.hash = '#/materials/graph/' + encodeURIComponent(id); },
    'publish-graph': async () => { const { materialId, diff } = state.review; await api.publish(materialId, diff.candidate_revision_id, diff.base_graph_version); closeModal(); await refreshData(); toast('知识图谱已发布，可以创建学习空间了。'); },
    'select-node': () => selectGraphNode(id),
    'graph-prev': () => { state.graphPage--; updateGraphExplorer(true); },
    'graph-next': () => { state.graphPage++; updateGraphExplorer(true); },
    'graph-clear-search': () => { state.graphQuery = ''; state.graphPage = 0; updateGraphExplorer(); $('[data-input="graph-search"]')?.focus(); },
    'graph-overview': () => { state.graphSelected = ''; state.graphQuery = ''; state.graphScope = 'overview'; state.graphPage = 0; state.graphHistory = []; updateGraphExplorer(true); },
    'graph-all': () => { state.graphSelected = ''; state.graphQuery = ''; state.graphScope = 'all'; state.graphPage = 0; state.graphHistory = []; updateGraphExplorer(true); },
    'graph-back': () => { const previous = state.graphHistory.pop(); if (previous) { state.graphSelected = previous.id; state.graphPage = previous.page; state.graphQuery = previous.query; state.graphScope = previous.scope; state.graphRelation = previous.relation; updateGraphExplorer(true); } },
    'reload-graph': () => { state.graphSpace = ''; state.graph = null; state.graphUiKey = ''; return state.mode === 'live' ? loadRoute() : render(); },
    'explore-topic': () => { state.graphSelected = id; state.graphMaterial = ''; state.graphSpace = currentSpace()?.id || ''; state.graph = null; state.graphUiKey = ''; go('graph'); },
    'ask-node': () => { const node = graphData().nodes.find(n => n.id === id); go('assistant'); setTimeout(() => { if ($('#question')) { $('#question').value = `请用直观的方式解释「${node.title || node.name}」，并给出资料依据。`; $('#question').focus(); } }, 0); },
    'node-source': () => { const node = graphData().nodes.find(n => n.id === id); return openSourcePage(node?.source_refs?.[Number(element.dataset.index)]); },
    'node-source-text': () => { const node = graphData().nodes.find(n => n.id === id); return openSourcePage(node?.source_refs?.[Number(element.dataset.index)]); },
    'chat-source': () => { const message = state.messages.get(currentSpace().id)[Number(element.dataset.message)]; return openSourcePage(message.citations[Number(element.dataset.index)]); },
    'retry-source': () => showSource(sourceReader.ref, { materialView: sourceReader.materialView, forceReader: sourceReader.materialView, allowUpload: sourceReader.allowUpload }),
    'open-source-pdf': () => sourceReader?.pdf && showSource(sourceReader.ref, { materialView: sourceReader.materialView }),
    'complete-task': () => completeTask(id), 'toggle-timer': toggleTimer, 'reset-timer': () => { resetTimer(); render(); },
    suggestion: () => { const input = $('#question'); input.value = element.dataset.text; input.focus(); },
    'resume-chat': () => sendChat('', true),
    'stop-chat': async () => { state.chatController?.abort(); const job = state.pendingJob; if (job) { await api.cancelRun(job.run_id); state.pendingJob = null; } updateChat(); toast('已停止接收本次回答。'); },
  };
  if (actions[action]) await busy(element.tagName === 'BUTTON' && !['select-node', 'toggle-timer', 'reset-timer'].includes(action) ? element : null, actions[action]);
});

document.addEventListener('submit', async event => {
  const form = event.target; event.preventDefault();
  if (form.matches('[data-workspace-form]')) { await workspace.submit(new FormData(form)); return; }
  if (form.matches('[data-workspace-filter]')) { await workspace.filter(new FormData(form)); return; }
  if (form.id === 'learning-replay-form') {
    try { const values = new FormData(form), range = dateRange(values); await learning.replay({ limit: Number(values.get('limit')), minimum_delay_hours: Number(values.get('delay')), ...(range.from ? { from_time: range.from } : {}), ...(range.to ? { to_time: range.to } : {}) }); }
    catch (error) { const box = $('[data-learning-error]', form); box.textContent = error.message; box.hidden = false; }
    return;
  }
  if (form.id === 'learning-start-form') {
    const setup = Object.fromEntries(new FormData(form));
    learning.configure(setup); await learning.start(setup);
    if (!learning.snapshot().error) $('#learning-answer-form input, #learning-answer-form textarea')?.focus();
    return;
  }
  if (form.id === 'learning-answer-form' || form.id === 'learning-comparison-form') {
    const values = new FormData(form);
    if (form.id === 'learning-answer-form') {
      await learning.answer(values.get('answer'));
      if (route().name === 'learning' && !learning.snapshot().error) $('#learning-answer-form input, #learning-answer-form textarea, [data-action=learning-finalize]')?.focus();
    } else {
      try { await learning.compare(comparisonPayload(values.get('budgets'), values.get('horizon'))); }
      catch (error) { const box = $('[data-learning-error]', form); if (box) { box.textContent = error.message; box.hidden = false; } }
    }
    return;
  }
  const submit = $('button[type=submit]', form);
  await busy(submit, async () => {
    const error = $('[data-form-error]', form); if (error) error.hidden = true;
    if (form.id === 'space-change-form') await submitSpaceChange(form);
    if (form.id === 'new-space-form') await createSpace(form);
    if (form.id === 'plan-form') await createPlan(form);
    if (form.id === 'connection-form') {
      const previous = token; token = new FormData(form).get('token').trim();
      try { await api.spaces(); } catch (error) { token = previous; throw error; }
      learning.clear(); state.chatController?.abort(); state.mode = 'live'; state.error = ''; state.selected = ''; state.graph = null; state.graphSpace = ''; state.topics.clear(); state.plans.clear(); state.messages.clear(); state.chats.clear(); state.pendingJob = null;
      storageWrite(session, TOKEN_KEY, token); storageWrite(session, MODE_KEY, 'live'); closeModal(); await refreshData(); toast('已连接本地服务，正在使用真实学习数据。');
    }
    if (form.id === 'scope-form') {
      const values = new FormData(form), ids = values.getAll('topic_ids'), excluded = values.getAll('excluded_topic_ids'), prerequisites = values.get('prerequisites') === 'on', space = byId(form.dataset.space);
      if (!ids.length) throw new Error('请选择至少一个学习主题。');
      if (ids.some(id => excluded.includes(id))) throw new Error('同一个主题不能同时纳入和排除。');
      if (state.mode === 'demo') { space.topic_ids = ids; space.excluded_topic_ids = excluded; space.include_prerequisites = prerequisites; space.space_version++; saveDemo(); }
      else { const result = await api.setScope(space, ids, excluded, prerequisites); Object.assign(space, result); state.plans.delete(space.id); learning.clear(); }
      const returnToPlan = form.dataset.returnPlan === 'true';
      closeModal();
      if (returnToPlan) { state.spaceTab = 'tasks'; state.spaceTabSpace = space.id; openSpaceTab('tasks', space.id); toast('学习范围已保存，继续生成学习任务。'); planModal(); }
      else { render(); toast('学习范围已保存，记得重新生成相关学习任务。'); await loadRoute(); }
    }
    if (form.id === 'chat-form') await sendChat(new FormData(form).get('question').trim());
  });
});
document.addEventListener('input', event => {
  const action = event.target.dataset.input;
  if (action === 'graph-search') { state.graphQuery = event.target.value; state.graphPage = 0; updateGraphExplorer(); }
  if (action === 'space-search') { state.query = event.target.value; $('#space-results').innerHTML = spaceResults(); }
  if (action === 'material-search') { state.query = event.target.value; $('#material-results').innerHTML = materialResults(); }
  if (action === 'global-search') $('#global-results').innerHTML = searchResults(event.target.value);
  if (event.target.closest('#plan-form')) updatePlanPreview(event.target.form);
});
document.addEventListener('change', async event => {
  if (event.target.matches('[data-chat-material]')) { state.chatMaterials.set(currentSpace().id, [...document.querySelectorAll('[data-chat-material]:checked')].map(input => input.value)); return; }
  if (event.target.closest('[data-workspace-form]')) {
    if (event.target.matches('[data-workspace-target]')) $('[data-workspace-correction-fields]', event.target.form).innerHTML = correctionFields(workspace.snapshot().data, event.target.value);
    updateWorkspaceConditions(main, workspace.snapshot());
    return;
  }
  if (event.target.id === 'source-pdf-input') { await attachSourcePdf(event.target.files?.[0]); return; }
  if (event.target.dataset.change === 'select-graph-context') {
    const [type, id] = event.target.value.split(':');
    state.graphMaterial = type === 'material' ? id : '';
    state.graphSpace = type === 'space' ? id : '';
    state.graph = null; state.graphUiKey = ''; state.graphSelected = ''; state.graphHistory = []; state.graphPage = 0; state.graphError = ''; render(); await loadRoute(); return;
  }
  if (event.target.dataset.change === 'graph-relation') { state.graphRelation = event.target.value; state.graphPage = 0; updateGraphExplorer(); $('[data-change="graph-relation"]')?.focus(); return; }
  if (event.target.closest('#learning-start-form')) learning.configure(Object.fromEntries(new FormData(event.target.form)));
  if (event.target.id === 'file-input') { try { await uploadFiles(event.target.files); } catch (error) { toast(error.message, true); } }
  if (event.target.dataset.change === 'select-space') {
    if (state.sending) { toast('请先等待当前回答完成，或停止接收。'); event.target.value = state.selected; return; }
    state.selected = event.target.value; state.spaceTab = 'overview'; state.spaceTabSpace = state.selected; state.graphSelected = ''; state.graphSpace = ''; state.graph = null; state.zoom = 1; state.pan = { x: 0, y: 0 }; state.planError = ''; render(); await loadRoute();
  }
  if (event.target.closest('#plan-form')) updatePlanPreview(event.target.form);
});
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchModal(); }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && event.target.id === 'question') { event.preventDefault(); $('#chat-form')?.requestSubmit(); }
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
window.addEventListener('beforeunload', () => { learning.dispose(); state.chatController?.abort(); clearInterval(state.timerInterval); });
state.selected = storageRead(session, `knowpath-selected-${state.mode}`, '') || data().spaces[0]?.id || '';
render();
if (state.mode === 'live') void refreshData();
