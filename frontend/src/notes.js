import { esc, dateText } from './store.js';
import { spacePath } from './workbench.js';

const rows = value => Array.isArray(value) ? value : [];
const status = value => ({ IN_PROGRESS: '进行中', COMPLETED: '已完成', pending: '待生成', queued: '待生成', generating: '生成中', completed: '已生成', failed: '失败', cancelled: '已取消' })[value] || (['未总结', '待总结', '待生成', '生成中', '已生成', '失败', '已取消'].includes(value) ? value : '未总结');
export const notePath = (spaceId, chapterId, revisionId) => spacePath(spaceId, 'notes', { chapter: chapterId, revision: revisionId });

// Escape before formatting; raw HTML and executable link schemes never reach the DOM.
export function renderNoteMarkdown(markdown = '') {
  const inline = text => {
    const codes = [];
    let safe = esc(text).replace(/`([^`]+)`/g, (_, code) => { codes.push(`<code>${code}</code>`); return `\u0000${codes.length - 1}\u0000`; });
    safe = safe.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, (_, label, url) => /^(https?:\/\/|#\/)/i.test(url) ? `<a href="${url}" rel="noopener noreferrer">${label}</a>` : label);
    safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\*([^*]+)\*/g, '<em>$1</em>');
    return safe.replace(/\u0000(\d+)\u0000/g, (_, index) => codes[Number(index)]);
  };
  const output = [], lines = String(markdown).split(/\r?\n/); let code = null, list = false;
  const closeList = () => { if (list) { output.push('</ul>'); list = false; } };
  for (const line of lines) {
    if (/^```/.test(line)) { closeList(); if (code === null) code = []; else { output.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = null; } continue; }
    if (code !== null) { code.push(line); continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line), item = /^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
    if (item) { if (!list) { output.push('<ul>'); list = true; } output.push(`<li>${inline(item[1])}</li>`); }
    else { closeList(); if (heading) output.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); else if (line.trim()) output.push(`<p>${inline(line)}</p>`); }
  }
  closeList(); if (code !== null) output.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  return output.join('');
}

export function createNotesController({ api, changed = () => {}, delay = 5000 }) {
  const retryKeys = new Map();
  let state = { key: '', busy: false, error: '', notebook: null, chapter: null, revisions: [], draft: null, editing: false, historical: false }, route, epoch = 0, timer, request;
  const snapshot = () => state;
  const stop = () => { clearTimeout(timer); timer = null; request?.abort(); request = null; };
  async function load(target, { refresh = false } = {}) {
    const key = JSON.stringify(target); const same = key === state.key;
    if (!refresh && same) return;
    stop(); const ticket = ++epoch; route = target;
    if (!same) state = { key, busy: true, error: '', notebook: null, chapter: null, revisions: [], draft: null, editing: false, historical: Boolean(target.query?.revision) };
    else state = { ...state, busy: true, error: '' };
    request = new AbortController(); const options = { signal: request.signal }; changed();
    try {
      if (target.name === 'notes') { const result = await api.notebooks(options); if (ticket === epoch) state.notebooks = rows(result.items); }
      else {
        const notebook = await api.notebook(target.id, options); if (ticket !== epoch) return; state.notebook = notebook;
        if (target.query?.chapter) {
          const chapterId = target.query.chapter;
          const [chapter, revisions] = await Promise.all([target.query.revision ? api.noteRevision(chapterId, target.query.revision, options) : api.noteChapter(chapterId, options), api.noteRevisions(chapterId, options)]);
          if (ticket !== epoch) return;
          const snapshot = { ...(chapter.snapshot || chapter), id: chapter.chapter_id || chapter.snapshot?.id || chapter.id };
          if (snapshot.space_id !== target.id) throw new Error('笔记章节与学习空间不一致。');
          state.chapter = snapshot; state.revisions = rows(Array.isArray(revisions) ? revisions : revisions.items);
        }
      }
    } catch (error) { if (ticket === epoch && error.name !== 'AbortError') state.error = error.message; }
    finally { if (ticket === epoch) { state.busy = false; changed(); if (!state.editing && !state.historical && rows(state.chapter?.generations).some(g => ['pending', 'generating'].includes(g.status))) timer = setTimeout(() => void load(route, { refresh: true }), delay); } }
  }
  function beginEdit() { if (!state.chapter || state.historical) return; stop(); state.editing = true; state.draft = structuredClone(rows(state.chapter.blocks)); state.draftCorrections = structuredClone(rows(state.chapter.corrections)); state.expectedVersion = state.chapter.version; changed(); }
  function editCorrection(id, markdown) { const correction = state.draftCorrections?.find(c => c.id === id); if (correction) correction.markdown = markdown; }
  function mergeLatest() {
    if (!state.editing) return;
    const present = new Set(state.draft.map(b => b.id));
    state.draft = [...state.draft.filter(b => b.kind === 'personal' || rows(state.chapter.blocks).some(current => current.id === b.id)), ...structuredClone(rows(state.chapter.blocks).filter(b => !present.has(b.id)))];
    state.draftCorrections = rows(state.chapter.corrections).map(c => ({ ...c, markdown: state.draftCorrections?.find(d => d.id === c.id)?.markdown ?? c.markdown }));
    state.expectedVersion = state.chapter.version; state.error = ''; changed();
  }
  function edit(id, markdown, correctionId) {
    if (!state.editing) beginEdit();
    const block = state.draft?.find(b => b.id === id); if (!block) return;
    if (correctionId) { const correction = rows(block.corrections).find(c => c.id === correctionId); if (correction) correction.explanation = markdown; }
    else block.markdown = markdown;
  }
  function addPersonal() { if (!state.editing) beginEdit(); state.draft.push({ id: crypto.randomUUID(), kind: 'personal', markdown: '', source_refs: [], corrections: [] }); changed(); }
  async function save() {
    if (!state.editing || state.busy) return; const ticket = epoch;
    state.busy = true; state.error = ''; changed();
    try { await api.updateNoteChapter(state.chapter.id, { expected_version: state.expectedVersion, blocks: state.draft.map(({id,kind,markdown}) => ({id,kind,markdown})), corrections: rows(state.draftCorrections).map(({id,markdown}) => ({id,markdown})) }); if (ticket !== epoch) return; state.draft = null; state.editing = false; await load(route, { refresh: true }); }
    catch (error) { if (ticket === epoch) state.error = error.status === 409 ? '章节已有新版本。你的修改已保留，请查看最新内容后合并再保存。' : error.message; }
    finally { if (ticket === epoch) { state.busy = false; changed(); } }
  }
  async function retry(id) { if (state.busy) return; const ticket = epoch; state.busy = true; changed(); if (!retryKeys.has(id)) retryKeys.set(id, crypto.randomUUID()); try { await api.retryNoteGeneration(id, { key: retryKeys.get(id) }); retryKeys.delete(id); if (ticket === epoch) await load(route, { refresh: true }); } catch (error) { if (ticket === epoch) state.error = error.message; } finally { if (ticket === epoch) { state.busy = false; changed(); } } }
  function dispose() { stop(); epoch++; state.key = ''; }
  function cancelEdit() { state.draft = null; state.editing = false; void load(route, { refresh: true }); }
  return { snapshot, load, refresh: () => load(route, { refresh: true }), edit, editCorrection, mergeLatest, beginEdit, addPersonal, save, retry, cancelEdit, dispose };
}

function renderGenerationDraft(snapshot) {
  const elapsed = snapshot.learning_time?.elapsed_seconds;
  return `<details class="workspace-details" open><summary>结构化草稿 · 正文待生成</summary><p>学习时间：${elapsed == null ? '未记录' : Number(elapsed) + ' 秒'}（包含暂停）</p><p>本次涉及主题：${rows(snapshot.topic_ids).map(esc).join('、') || '未记录'}</p><h3>已记录测评结果</h3>${rows(snapshot.assessment_result?.question_results).map(q => `<p>${esc(q.question_id)} · ${esc(q.verdict || '未验证')} · ${q.score == null ? '分数未记录' : '得分 ' + esc(q.score)}${q.assisted ? ' · 有辅助记录' : ''}</p>`).join('') || '<p>暂无逐题结果。</p>'}<p class="subtle">这些是后端记录的事实，尚未生成笔记正文；测评得分不等同于掌握状态。</p></details>`;
}

export function renderNotes(state) {
  const action = (text, name, attrs = '') => `<button type="button" class="button button-secondary" data-action="notes-${name}" ${attrs} ${state.busy ? 'disabled' : ''}>${text}</button>`;
  const error = state.error ? `<div class="notice error" role="alert">${esc(state.error)}</div>` : '';
  const loading = state.busy && !state.notebook && !state.notebooks ? '<p role="status">正在读取学习笔记…</p>' : '';
  if (!state.notebook) return `<section class="panel notes-page"><h1>学习笔记</h1><p class="subtle">按学习空间整理，关联测评完成后自动总结。笔记保存在后端数据库中。</p>${error}${loading}${rows(state.notebooks).length ? rows(state.notebooks).map(book => `<article class="workspace-question"><h2><a href="${notePath(book.space_id)}">${esc(book.name)}</a></h2><p>${esc(book.directory || '')}</p><p>${Number(book.completed_count || 0)} / ${Number(book.chapter_count || book.total_count || 0)} 个章节已完成 · ${Number(book.correction_count || 0)} 项纠偏</p></article>`).join('') : !state.busy ? '<p class="record-empty">还没有学习笔记册。创建学习计划并完成关联测评后，会自动整理笔记。</p>' : ''}${action('刷新', 'refresh')}</section>`;
  const book = state.notebook, chapters = rows(book.chapters), completed = chapters.filter(c => c.status === 'COMPLETED').length;
  const index = `<section class="panel notes-index"><h2>README · ${esc(book.name)}</h2><p class="subtle">${esc(book.directory)}</p><p>${completed} / ${chapters.length} 已完成 · ${chapters.reduce((n,c) => n + Number(c.correction_count || 0), 0)} 项纠偏</p><progress max="${Math.max(1, chapters.length)}" value="${completed}" aria-label="笔记章节完成进度"></progress>${chapters.length ? `<ul>${chapters.map(c => `<li><a href="${notePath(book.space_id, c.id)}">${esc(c.number)} ${esc(c.title)}</a><span class="badge">${status(c.status)}</span><small>${status(c.generation_status)} · 纠偏 ${Number(c.correction_count || 0)} · ${dateText(c.updated_at)}</small></li>`).join('')}</ul>` : '<p>尚未创建计划节点；章节正文不会预填未学习内容。</p>'}${book.readme_markdown ? `<details><summary>查看 README</summary>${renderNoteMarkdown(book.readme_markdown)}</details>` : ''}</section>`;
  const c = state.chapter;
  if (!c) return `<div class="notes-page">${error}${action('刷新', 'refresh')}${index}</div>`;
  const blocks = state.editing ? state.draft : rows(c.blocks);
  const body = rows(blocks).map((block, blockIndex) => `<article class="note-block" data-block="${esc(block.id)}">${block.evidence_validity === 'stale' ? '<div class="notice warning" role="status">本段依据已失效，保留为历史学习记录；不能视为当前已验证知识，请重新测评。</div>' : ''}${state.historical && block.kind === 'assessment' ? '<p class="subtle">历史版本反映当时的记录，不代表当前证据仍然有效。</p>' : ''}${state.editing ? `<label>章节正文<textarea data-note-block="${esc(block.id)}" rows="10">${esc(block.markdown || '')}</textarea></label>` : `<div class="note-markdown">${renderNoteMarkdown(block.markdown || '')}</div>`}${rows(block.corrections).map(correction => `<div class="note-correction"><strong>${correction.confirmed || correction.eligible ? '已确认纠偏' : '纠偏说明'}</strong>${state.editing ? `<textarea data-note-block="${esc(block.id)}" data-note-correction="${esc(correction.id)}">${esc(correction.explanation || correction.description || '')}</textarea>` : `<p>${esc(correction.explanation || correction.description || '')}</p>`}</div>`).join('')}${rows(block.source_refs).map((ref, sourceIndex) => action(`来源 ${sourceIndex + 1}`, 'source', `data-block-index="${blockIndex}" data-source-index="${sourceIndex}"`)).join('')}</article>`).join('');
  return `<div class="notes-page">${error}${index}<section class="panel notes-chapter">${c.error ? `<div class="notice error" role="alert">${esc(typeof c.error === 'string' ? c.error : c.error.message || c.error.code || '生成失败')}</div>` : ''}<div class="panel-title"><h2>${esc(c.number)} ${esc(c.title)}</h2><span class="badge">${status(c.status)}</span></div><p class="subtle">${esc(c.filename)} · 版本 ${Number(c.version)}${state.historical ? ' · 历史版本（只读）' : ''}</p><p>学习时间：${c.elapsed_seconds == null ? '未记录' : Math.round(Number(c.elapsed_seconds) / 60) + ' 分钟'}（起止间隔包含暂停） · 纠偏 ${Number(c.correction_count || 0)}</p>${rows(c.generations).map(g => `<p role="status">${status(g.status)}${g.error ? ' · ' + esc(typeof g.error === 'string' ? g.error : g.error.message || '') : ''}${g.status === 'failed' && !state.historical ? action('重试生成', 'retry', `data-id="${esc(g.id)}"`) : ''}</p>${g.snapshot && ['pending', 'generating', 'failed'].includes(g.status) ? renderGenerationDraft(g.snapshot) : ''}`).join('')}<div class="record-actions">${state.historical ? `<a class="text-link" href="${notePath(c.space_id, c.id)}">返回当前章节</a>` : state.editing ? action('保存修改', 'save') + action('取消编辑', 'cancel') + action('添加个人段落', 'personal') + action('查看最新内容（保留修改）', 'refresh') : action('编辑笔记', 'edit') + action('刷新', 'refresh')}</div>${state.editing && c.version !== state.expectedVersion ? `<details open><summary>服务器最新内容（请比较后合并）</summary>${rows(c.blocks).map(b => renderNoteMarkdown(b.markdown)).join('')}${action('合并最新版本并保留我的修改', 'merge')}</details>` : ''}${rows(state.editing ? state.draftCorrections : c.corrections).map(correction => `<div class="note-correction"><strong>${correction.confirmed ? '已确认纠偏' : '纠偏依据待验证'}</strong><p class="subtle">${correction.independent_retest_passed ? '独立复测已通过' : '纠偏说明不代表独立复测通过'}</p>${state.editing ? `<textarea data-note-correction-id="${esc(correction.id)}">${esc(correction.markdown || '')}</textarea>` : renderNoteMarkdown(correction.markdown || '')}</div>`).join('')}${body || '<p class="record-empty">待总结。完成关联测评后只追加已学习、已验证的内容。</p>'}<details class="workspace-details"><summary>版本历史</summary>${rows(state.revisions).map(r => `<p><a href="${notePath(c.space_id, c.id, r.id)}">版本 ${Number(r.version)} · ${dateText(r.created_at || r.updated_at)} · ${esc(r.kind || r.reason || '')}</a></p>`).join('') || '<p>暂无历史版本。</p>'}</details></section></div>`;
}
