import { esc, storageRead, storageWrite } from './store.js';

export function scopeModel(topics, space = {}) {
  const byId = new Map(topics.map(t => [t.id, t])), units = [], seen = new Set();
  for (const topic of topics) {
    const id = topic.canonical_topic_id || topic.id;
    if (seen.has(id) || topic.is_learnable === false || (topic.status && topic.status !== 'active')) continue;
    seen.add(id); units.push(byId.get(id) || { ...topic, id });
  }
  const normalize = ids => new Set((ids || []).map(id => byId.get(id)?.canonical_topic_id || id).filter(id => seen.has(id)));
  return { topics, units, selected: normalize(space.topic_ids), excluded: normalize(space.excluded_topic_ids) };
}
export function scopeBatch(model, { parent, material, query = '', selected = true } = {}) {
  const byId = new Map(model.topics.map(t => [t.id, t]));
  const under = topic => {
    const visited = new Set(); let current = topic;
    while (current && !visited.has(current.id)) {
      if (current.id === parent) return true;
      visited.add(current.id); current = byId.get(current.parent_id);
    }
    return false;
  };
  for (const topic of model.units) {
    if (parent && !under(topic)) continue;
    if (material && !(topic.source_refs || []).some(r => r.material_id === material)) continue;
    if (query && !String(topic.name || topic.title).toLocaleLowerCase().includes(query.toLocaleLowerCase())) continue;
    if (selected) { model.selected.add(topic.id); model.excluded.delete(topic.id); }
    else model.selected.delete(topic.id);
  }
  return model;
}
const title = t => t?.title || t?.name || t?.id || '';
export function renderScopePicker(topics, space, materials = []) {
  const model = scopeModel(topics, space), all = space.scope_mode === 'all';
  const groups = topics.filter(t => t.is_learnable === false && (t.content_role === 'container' || t.kind === 'chapter') && (!t.canonical_topic_id || t.canonical_topic_id === t.id) && scopeBatch({ ...model, selected: new Set(), excluded: new Set() }, { parent: t.id }).selected.size > 0);
  const batch = (label, attrs = '', unit = '整章') => `<span>${esc(label)}</span> <button type="button" class="text-link" data-scope-batch="select" ${attrs}>全选${unit}</button> <button type="button" class="text-link" data-scope-batch="clear" ${attrs}>取消${unit}</button>`;
  const names = new Map(topics.map(t => [t.id, title(t)]));
  return `<div data-scope-picker data-topics="${esc(JSON.stringify(topics))}"><fieldset class="form-field"><legend>范围方式</legend><label class="check-label"><input type="radio" name="scope_mode" value="all" ${all ? 'checked' : ''}>学习全部有效内容</label><label class="check-label"><input type="radio" name="scope_mode" value="custom" ${all ? '' : 'checked'}>自选章节</label></fieldset><p class="subtle">全部有效内容包含可学正文与直属导言；章节容器不作为学习任务。自选范围清空后不能保存。</p><label class="form-field">搜索学习单元<input type="search" data-scope-search placeholder="按名称搜索"></label><p><button type="button" class="text-link" data-scope-batch="select" data-scope-results>全选仅搜索结果</button> · <button type="button" class="text-link" data-scope-batch="clear" data-scope-results>取消仅搜索结果</button></p><p class="subtle">搜索批选仅作用于搜索结果，不清除隐藏已选。资料和章节按钮始终作用于整份资料或整章。</p><div class="material-choices" style="max-height:160px">${materials.map(m => `<p>${batch(m.name || m.id, `data-scope-material="${esc(m.id)}"`, '整份资料')}</p>`).join('')}${groups.map(t => `<p>${batch(title(t), `data-scope-parent="${esc(t.id)}"`)}</p>`).join('')}</div><p data-scope-summary role="status" aria-live="polite">已选 ${model.selected.size} / ${model.units.length} 个学习单元 · 预计基础阅读 ${model.selected.size * 10} 分钟（每单元约 10 分钟，不含测评与个性化调整，实际以计划为准）</p><div class="material-choices" style="max-height:280px">${model.units.map(t => `<div data-scope-row data-scope-name="${esc(title(t).toLocaleLowerCase())}"><label class="check-label"><input type="checkbox" name="topic_ids" value="${esc(t.id)}" ${model.selected.has(t.id) ? 'checked' : ''}>${esc(title(t))}</label><label class="check-label"><input type="checkbox" name="excluded_topic_ids" value="${esc(t.id)}" ${model.excluded.has(t.id) ? 'checked' : ''}>暂时排除 ${esc(title(t))}</label></div>`).join('') || '<p>没有可学习的有效内容，请先检查资料的解析与发布状态。</p>'}</div><label class="check-label"><input type="checkbox" name="prerequisites" ${space.include_prerequisites !== false ? 'checked' : ''}>自动纳入必要的前置知识</label><p class="subtle">未掌握的硬前置必须纳入范围；有效已掌握前置可满足要求，软建议不会强制加入。</p>${(space.prerequisite_additions || []).map(a => `<p class="subtle">自动加入 ${esc(names.get(a.topic_id) || a.topic_id)}：${esc(a.reason)}，用于 ${esc(names.get(a.required_by_topic_id) || a.required_by_topic_id)}；来源 ${(a.source_refs || []).map(r => esc(materials.find(m => m.id === r.material_id)?.name || r.material_id) + (r.page ? ' 第 ' + esc(r.page) + ' 页' : r.line_start ? ' 第 ' + esc(r.line_start) + ' 行' : ' 原文片段')).join('、')}</p>`).join('')}</div>`;
}

export function updateScopePicker(root, target) {
  const picker = target?.closest?.('[data-scope-picker]');
  if (!picker || !root.contains(picker)) return false;
  const selected = [...picker.querySelectorAll('[name="topic_ids"]')], excluded = [...picker.querySelectorAll('[name="excluded_topic_ids"]')];
  const radio = mode => { const input = picker.querySelector(`[name="scope_mode"][value="${mode}"]`); if (input) input.checked = true; };
  if (target.matches('[name="topic_ids"], [name="excluded_topic_ids"]')) {
    if (target.checked) for (const input of (target.name === 'topic_ids' ? excluded : selected)) if (input.value === target.value) input.checked = false;
    radio('custom');
  }
  if (target.matches('[name="scope_mode"][value="all"]') && target.checked) { selected.forEach(i => { i.checked = true; }); excluded.forEach(i => { i.checked = false; }); }
  const query = picker.querySelector('[data-scope-search]').value.toLocaleLowerCase();
  for (const row of picker.querySelectorAll('[data-scope-row]')) row.hidden = !row.dataset.scopeName.includes(query);
  const batch = target.closest('[data-scope-batch]');
  if (batch) {
    // Hierarchy membership comes from the bound snapshot, serialized on the picker.
    const topics = JSON.parse(picker.dataset.topics || '[]');
    const model = scopeModel(topics, { topic_ids: selected.filter(i => i.checked).map(i => i.value), excluded_topic_ids: excluded.filter(i => i.checked).map(i => i.value) });
    scopeBatch(model, { parent: batch.dataset.scopeParent, material: batch.dataset.scopeMaterial, query: batch.hasAttribute('data-scope-results') ? query : '', selected: batch.dataset.scopeBatch === 'select' });
    selected.forEach(i => { i.checked = model.selected.has(i.value); }); excluded.forEach(i => { i.checked = model.excluded.has(i.value); }); radio('custom');
  }
  const count = selected.filter(i => i.checked).length;
  picker.querySelector('[data-scope-summary]').textContent = `已选 ${count} / ${selected.length} 个学习单元 · 预计基础阅读 ${count * 10} 分钟（每单元约 10 分钟，不含测评与个性化调整，实际以计划为准）`;
  return true;
}
export function scopePayload(values) {
  const topic_ids = values.getAll('topic_ids'), excluded_topic_ids = values.getAll('excluded_topic_ids');
  if (!topic_ids.length) throw new Error('自选范围为空。请选择至少一个学习单元，或选择“学习全部有效内容”。');
  if (topic_ids.some(id => excluded_topic_ids.includes(id))) throw new Error('同一个学习单元不能同时纳入和排除。');
  return { topic_ids, excluded_topic_ids, include_prerequisites: values.get('prerequisites') === 'on', scope_mode: values.get('scope_mode') || 'custom' };
}
export function createScopeSaver({ api, storage }) {
  const journal = 'knowpath-scope-commands-v1';
  let fallback = {}, unsaved = false;
  const read = () => unsaved ? fallback : storageRead(storage, journal, {});
  const write = pending => { fallback = pending; unsaved = !storageWrite(storage, journal, pending); };
  return async (space, body) => {
    const pending = read(), recovering = Boolean(pending[space.id]);
    const entry = pending[space.id] || { key: crypto.randomUUID(), body: { ...structuredClone(typeof body === 'function' ? body() : body), expected_version: space.space_version } };
    pending[space.id] = entry; write(pending);
    const clear = () => { const latest = read(); if (latest[space.id]?.key === entry.key) delete latest[space.id]; write(latest); };
    try {
      const b = entry.body;
      const result = await api.setScope({ id: space.id, space_version: b.expected_version }, b.topic_ids, b.excluded_topic_ids, b.include_prerequisites, { key: entry.key, scopeMode: b.scope_mode });
      clear(); return recovering ? { ...result, recovered_scope_command: true } : result;
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) clear();
      throw error;
    }
  };
}
