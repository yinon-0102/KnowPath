import { esc } from './store.js';
import { taskDisplay } from './progress.js';
const nodeId = task => task.node_id || task.context?.node_id || task.id;
const movable = task => !task.historical && !task.context?.historical && !['completed','skipped','in_progress','active'].includes(task.status) && !task.active_session_id;
export function selectAssessmentOption(row, target, eventFactory = () => new Event('change', { bubbles: true })) {
  if (target.closest('a,button,input,label,select,textarea')) return false;
  const radio = row.querySelector('input[type="radio"]');
  if (!radio || radio.matches(':disabled')) return false;
  radio.checked = true; radio.dispatchEvent(eventFactory()); return true;
}
export function assessmentStageText(stage, startedAt, now = Date.now()) {
  const text = {waiting:'等待出题',generating:'生成题目',validating:'验证题目',ready:'题目已就绪'}[stage] || '查询出题进度';
  return text + (startedAt ? ` · 已等待 ${Math.max(0,Math.floor((now - startedAt)/1000))} 秒` : '');
}
export function createPlanOrderController({ api, uid = () => crypto.randomUUID() }) {
  let state, epoch = 0;
  const keys = new Map();
  const snapshot = () => state;
  const payload = () => ({expected_plan_version:state.plan.version,order_mode:state.order_mode,material_order:[...state.material_order],...(state.order_mode === 'custom' ? {ordered_node_ids:[...state.ordered_node_ids]} : {})});
  function open(plan, space, activeSession = null) {
    epoch++;
    if (activeSession?.status === 'active' && activeSession.plan_id === (plan.id || plan.plan_id)) {
      plan = { ...plan, tasks: (plan.tasks || []).map(task => task.id === activeSession.task_id ? { ...task, active_session_id: activeSession.id || activeSession.session_id } : task) };
    }
    const bound = [...new Set(space.material_ids || (space.bindings || []).map(b=>b.material_id))];
    const priority = (plan.config?.material_order || []).filter(id=>bound.includes(id));
    const tasks = [...(plan.tasks || [])].sort((a,b)=>(a.context?.position ?? a.sequence ?? 0)-(b.context?.position ?? b.sequence ?? 0));
    state = {plan,space,order_mode:plan.config?.order_mode || 'source',material_order:[...priority,...bound.filter(id=>!priority.includes(id))],ordered_node_ids:tasks.filter(movable).map(nodeId),preview:null,busy:false,error:''};
    return state;
  }
  function configure(values) { Object.assign(state,values); state.preview=null; state.error=''; epoch++; }
  function move(kind,id,direction) {
    const field = kind === 'material' ? 'material_order' : 'ordered_node_ids';
    const items=[...state[field]], index=items.indexOf(id), next=index+direction;
    if(index<0 || next<0 || next>=items.length || state.busy) return;
    [items[index],items[next]]=[items[next],items[index]]; configure({[field]:items});
  }
  async function preview() {
    const ticket=++epoch, body=payload(); state.busy=true; state.error=''; state.preview=null;
    try {
      const result=await api.orderPreview(state.plan.id || state.plan.plan_id,body);
      if(ticket!==epoch) return;
      if(result.plan_id !== (state.plan.id || state.plan.plan_id) || result.expected_plan_version !== body.expected_plan_version) throw new Error('预览版本与当前计划不一致，请重新读取计划。');
      state.preview=result; state.previewBody=body;
      // Backend is authoritative about fixed nodes, including paused sessions.
      if(result.fixed_nodes) state.ordered_node_ids=state.ordered_node_ids.filter(id=>!result.fixed_nodes.some(node=>node.node_id===id));
      return result;
    } catch(error) { if(ticket===epoch) state.error=error.message; throw error; }
    finally { if(ticket===epoch) state.busy=false; }
  }
  async function apply() {
    if(!state.preview?.can_apply || JSON.stringify(payload())!==JSON.stringify(state.previewBody)) throw new Error('请先预览当前顺序并解决冲突。');
    const ticket=epoch, body=state.previewBody, signature=JSON.stringify([state.plan.id,body]);
    if(!keys.has(signature)) keys.set(signature,uid());
    state.busy=true; state.error='';
    try { const result=await api.reorderPlan(state.plan.id || state.plan.plan_id,body,{key:keys.get(signature)}); if(ticket!==epoch) return; return result; }
    catch(error) { if(ticket===epoch) state.error=error.code==='VERSION_CONFLICT' ? '计划版本已变化，你调整的顺序仍保留。重新读取后再预览。' : error.message; throw error; }
    finally { if(ticket===epoch) state.busy=false; }
  }
  async function refresh() {
    const old=state, latest=await api.plan(old.plan.id || old.plan.plan_id);
    if(state!==old) return;
    const keep=old.ordered_node_ids, selected=old.order_mode, priority=old.material_order;
    open(latest,old.space); state.order_mode=selected;
    state.ordered_node_ids=[...keep.filter(id=>state.ordered_node_ids.includes(id)),...state.ordered_node_ids.filter(id=>!keep.includes(id))];
    state.material_order=[...priority.filter(id=>state.material_order.includes(id)),...state.material_order.filter(id=>!priority.includes(id))];
  }
  return {open,snapshot,configure,move,preview,apply,refresh};
}
const reasons={completed:'已完成',skipped:'已跳过',active_session:'活动会话',in_progress:'学习中',historical:'历史节点',fixed:'固定位置',prerequisite_adjustment:'为满足前置知识调整',source:'原文顺序',adaptive:'智能顺序',custom:'自定义顺序'};
export function renderPlanOrder(state, materials = []) {
  const moveButtons=(kind,id,index,count)=>`<span class="order-moves"><button type="button" data-action="order-move" data-kind="${kind}" data-id="${esc(id)}" data-direction="-1" aria-label="上移" ${state.busy || index===0 ? 'disabled' : ''}>↑</button><button type="button" data-action="order-move" data-kind="${kind}" data-id="${esc(id)}" data-direction="1" aria-label="下移" ${state.busy || index===count-1 ? 'disabled' : ''}>↓</button></span>`;
  const preview=state.preview, tasks=preview?.candidate_plan?.tasks || state.plan.tasks || [];
  const fixed=preview?.fixed_nodes || tasks.filter(task=>!movable(task)).map(task=>({node_id:nodeId(task),reason:task.active_session_id ? 'active_session' : task.status}));
  const custom=state.ordered_node_ids.map(id=>tasks.find(task=>nodeId(task)===id)).filter(Boolean);
  const suggestion = preview?.suggested_plan?.tasks;
  const suggestionMarkup = suggestion ? `<p class="notice" aria-label="可行顺序建议">建议可行顺序：${suggestion.map(task=>esc(taskDisplay(task).name)).join(' → ')}。可调整节点后重新预览，或切换原文顺序。</p>` : preview?.suggestion_unavailable_reason ? `<p class="subtle">${esc(preview.suggestion_unavailable_reason)}</p>` : '';
  const schedule=value=>(value || []).map(row=>`${row.scheduled_date || '未安排日期'} · ${row.estimated_minutes ?? '—'} 分钟`).join('；');
  const orderedTasks = [...tasks].sort((a,b)=>(a.context?.position ?? a.sequence ?? 0)-(b.context?.position ?? b.sequence ?? 0));
  const fixedById = new Map(fixed.map(item => [item.node_id, item]));
  const routeMarkup = orderedTasks.map((task,index) => {
    const id = nodeId(task), lock = fixedById.get(id), display = taskDisplay(task,index);
    const controls = lock ? `<span class="order-lock" title="${esc(reasons[lock.reason] || lock.reason || '固定位置')}">固定 · ${esc(reasons[lock.reason] || lock.reason || '固定位置')}</span>` : state.order_mode === 'custom' ? moveButtons('node', id, custom.indexOf(task), custom.length) : '<span class="order-lock">可在自定义顺序中移动</span>';
    return `<li class="order-route-row ${lock ? 'is-fixed' : ''}"><span class="order-slot">${String(index + 1).padStart(2,'0')}</span><span class="order-route-copy"><strong>${esc(display.name || display.title)}</strong><small>${esc(display.sourceLabel || '原文位置未记录')} · 预计 ${Number(task.estimated_minutes || task.minutes || 25)} 分钟</small></span>${controls}</li>`;
  }).join('');
  return `<div class="order-layout"><aside class="order-settings"><p class="order-kicker">安排方式</p><p class="modal-intro">调整当前任务的学习顺序。已完成、已跳过和活动会话节点保留位置，笔记编号保持稳定。</p><div class="order-mode-cards">${[['source','原文顺序','顺着资料的章节阅读，保留必要前置关系'],['adaptive','智能安排','结合掌握状态安排学习与复习'],['custom','自定义顺序','用右侧按钮移动可调整节点']].map(([id,label,description])=>`<label class="order-mode-card ${state.order_mode===id?'selected':''}"><input type="radio" name="order-mode" value="${id}" data-change="order-mode" ${state.order_mode===id?'checked':''} ${state.busy?'disabled':''}><span><strong>${label}</strong><small>${description}</small></span></label>`).join('')}</div>${state.material_order.length>1 ? `<section class="order-priority"><h3>资料优先级</h3><ol class="order-list">${state.material_order.map((id,index)=>`<li><span>${esc(materials.find(m=>m.id===id)?.name || id)}</span>${moveButtons('material',id,index,state.material_order.length)}</li>`).join('')}</ol></section>` : ''}<button type="button" class="text-link order-refresh-link" data-action="order-refresh" ${state.busy ? 'disabled' : ''}>重新读取计划</button></aside><section class="order-editor"><div class="order-editor-heading"><div><h3>这轮学习路线</h3><p>原文章节与路线序号分别标明；固定节点保留在原位置。</p></div><span class="order-state-tag">${preview ? (preview.can_apply ? '已通过预览' : '有冲突') : '待预览'}</span></div><ol class="order-route-list">${routeMarkup || '<li class="home-empty">暂无可调整节点。</li>'}</ol>${preview ? `<section class="order-preview" aria-label="顺序预览"><h3>预览结果</h3><p>原安排：${esc(schedule(preview.schedule_before || preview.before_schedule))}<br>新安排：${esc(schedule(preview.schedule_after || preview.after_schedule))}</p>${(preview.conflicts || []).map(item=>`<p class="notice error" role="alert">${esc(item.message || item.code || item)}</p>`).join('')}${suggestionMarkup}</section>` : '<p class="order-help">预览会检查必要前置知识与排期；通过后即可应用。</p>'}</section></div><p class="form-error" data-form-error role="alert" ${state.error ? '' : 'hidden'}>${esc(state.error)}</p><div class="form-actions"><button type="button" class="button button-secondary" data-action="close-modal">取消</button><button type="button" class="button button-secondary" data-action="order-preview" ${state.busy ? 'disabled' : ''}>${state.busy ? '处理中…' : '预览顺序'}</button><button type="button" class="button button-primary" data-action="order-apply" ${state.busy || !preview?.can_apply ? 'disabled' : ''}>应用顺序</button></div>`;
}
