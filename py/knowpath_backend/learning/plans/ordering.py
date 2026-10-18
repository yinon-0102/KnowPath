"""Pure source-aware topological ordering, independent of mastery identity."""
from datetime import datetime
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.spaces.scope import prerequisite_conflicts

ORDER_POLICY_VERSION = 'order-v1'


def material_order(space, requested=None):
    bound = list(dict.fromkeys(b['material_id'] for b in space.get('bindings', [])))
    result = bound if requested is None else requested
    if not isinstance(result, list) or any(not isinstance(i, str) for i in result) or len(result) != len(set(result)) or set(result) != set(bound):
        raise DomainConflict('INVALID_MATERIAL_ORDER', 'material_order 必须恰好包含所有绑定材料', {'material_ids': bound})
    return list(result)


def _locator(ref, source_positions):
    """Accept real numeric locators or positions proven by the bound snapshot."""
    page = ref.get("page")
    line = ref.get("line_start")
    page = page if type(page) is int and page > 0 else None
    line = line if type(line) is int and line > 0 else None
    positions = source_positions or {}
    material, version = ref.get("material_id"), ref.get("material_version_id")
    snapshot = None
    for identifier in (ref.get("source_unit_id"), ref.get("chunk_id")):
        if isinstance(identifier, str) and identifier:
            candidate = positions.get((material, version, identifier))
            if type(candidate) is int and candidate >= 0:
                snapshot = candidate
                break
    section = ref.get("section_path") or ref.get("section")
    if isinstance(section, (list, tuple)) and section and all(isinstance(part, str) and part.strip() for part in section):
        section = tuple(section)
    elif not isinstance(section, str) or not section.strip():
        section = None
    section_index = positions.get(("section", material, version, section)) if section is not None else None
    if type(section_index) is not int or section_index < 0:
        section, section_index = None, None
    return page, line, section, section_index, snapshot


def source_key(topic, materials, source_positions=None):
    ranks = {identifier: index for index, identifier in enumerate(materials)}
    infinity = 10**12
    keys = []
    for ref in topic.get('source_refs') or []:
        page, line, section, section_index, snapshot = _locator(ref, source_positions)
        known = any(value is not None for value in (page, line, section_index, snapshot))
        keys.append((ranks.get(ref.get('material_id'), len(ranks)), 0 if known else 1,
                     page if page is not None else infinity,
                     line if line is not None else infinity,
                     section_index if section_index is not None else (snapshot if snapshot is not None else infinity),
                     str(section or '')))
    located_keys = [key for key in keys if not key[1]] or keys
    located = min(located_keys) if located_keys else (len(ranks), 1, infinity, infinity, infinity, '')
    missing = not keys or all(key[1] for key in keys)
    return (int(missing), *located, str(topic.get('name') or '').casefold(), topic['id'])


def source_label(topic, materials=None, source_positions=None):
    refs = topic.get('source_refs') or []
    materials = materials or list(dict.fromkeys(ref.get('material_id') for ref in refs))
    refs = sorted(refs, key=lambda ref: source_key({'id': topic['id'], 'source_refs': [ref]}, materials, source_positions))
    for ref in refs:
        page, line, section, section_index, snapshot = _locator(ref, source_positions)
        if page is not None:
            return f"第 {page} 页"
        if line is not None:
            return f"第 {line} 行"
        if section_index is not None:
            return ' / '.join(section) if isinstance(section, tuple) else section
        if snapshot is not None:
            return '来源片段顺序'
    return '原文位置未记录'


def canonical_id(topic):
    return topic.get('canonical_topic_id') or topic['id']


def order_topics(topics, states, mode, materials, timestamp, *, custom_order=None, custom_alias_order=None, source_positions=None, all_topics=None):
    by_id = {topic['id']: topic for topic in topics}
    universe = {topic['id']: topic for topic in (all_topics or topics)}
    conflicts = prerequisite_conflicts(topics, all_topics or topics, states)
    if conflicts:
        raise DomainConflict('PLAN_CONSTRAINT_UNSATISFIABLE', '所选范围缺少必要前置知识',
                             {'conflicts': ['missing_prerequisite', *[c['prerequisite_topic_id'] for c in conflicts]], 'adjustable_constraints': ['将前置知识加入范围', '移除依赖主题']})
    ranks = {'needs_review': 0, 'unstable': 1, 'learning': 2, 'mastered': 3, 'unseen': 4}
    custom = {identifier: index for index, identifier in enumerate(custom_order or [])}
    aliases = {identifier: index for index, identifier in enumerate(custom_alias_order or [])}
    today = datetime.fromisoformat(timestamp).date()
    def priority(topic):
        source = source_key(topic, materials, source_positions)
        if mode == 'custom':
            return (custom.get(canonical_id(topic), len(custom)), aliases.get(topic['id'], len(aliases)), source)
        if mode != 'adaptive':
            return source
        state = states.get(topic['id'], {})
        status = state.get('status', 'unseen')
        due = state.get('review_due_at') or state.get('next_review_at')
        if status == 'mastered' and state.get('review_due', bool(due and datetime.fromisoformat(due).date() <= today)):
            status = 'needs_review'
        return (ranks.get(status, 4), -len(state.get('error_tags') or []), state.get('last_assessed_at') or '9999', source)
    remaining = dict(by_id)
    result = []
    while remaining:
        pending = {canonical_id(t) for t in remaining.values()}
        ready = [t for t in remaining.values() if all(canonical_id(universe.get(p, {'id': p})) not in pending
                                                      for p in t.get('prerequisites', []))]
        if not ready:
            raise DomainConflict('PLAN_CONSTRAINT_UNSATISFIABLE', '知识点前置关系存在环',
                                 {'conflicts': ['prerequisite_cycle'], 'adjustable_constraints': ['审核并修正前置关系']})
        topic = min(ready, key=priority)
        result.append(topic)
        del remaining[topic['id']]
    return result


def fill_ordered_slots(template, ordered, fixed_ids, states):
    """Keep frozen slots, reserving earlier slots for their unmet ancestors."""
    remaining = list(ordered)
    by_topic = {i: t for t in template if not t.get("context", {}).get("historical") for i in t["topic_ids"]}
    satisfied = {i for i, t in by_topic.items() if t["status"] == "completed" or states.get(i, {}).get("status") == "mastered"}
    result = []
    for position, slot in enumerate(template):
        if slot["id"] in fixed_ids:
            chosen = slot
        else:
            ready = [t for t in remaining if t["status"] == "deferred" or all(
                p in satisfied for p in t.get("context", {}).get("prerequisites", []))]
            if not ready:
                # Preserve candidate for explicit conflict reporting instead of
                # inventing an edge or silently dropping a blocked task.
                ready = list(remaining)
            next_fixed = next((index for index in range(position + 1, len(template))
                               if template[index]["id"] in fixed_ids and template[index]["status"] not in {"completed", "skipped"}
                               and not template[index].get("context", {}).get("historical")), None)
            required = set()
            if next_fixed is not None:
                stack = list(template[next_fixed].get("context", {}).get("prerequisites", []))
                while stack:
                    parent = stack.pop()
                    task = by_topic.get(parent)
                    if parent in satisfied or task is None or task["id"] in required:
                        continue
                    required.add(task["id"])
                    stack.extend(task.get("context", {}).get("prerequisites", []))
                free_slots = sum(t["id"] not in fixed_ids for t in template[position:next_fixed])
                must_choose = [t for t in ready if t["id"] in required]
                if len(required) >= free_slots and must_choose:
                    ready = must_choose
            chosen = ready[0]
            remaining.remove(chosen)
        result.append(chosen)
        if chosen["status"] not in {"deferred", "skipped"} and not chosen.get("context", {}).get("historical"):
            satisfied.update(chosen["topic_ids"])
    return result



def reorder_task_nodes(tasks, topics, states, config, *, active_node_ids=None, source_positions=None):
    """Restore surviving custom nodes before assigning their new plan slots."""
    preference = {node: index for index, node in enumerate(config.get('custom_node_order', []))}
    by_topic = {t['id']: t for t in topics}
    def priority(task):
        node = task.get('context', {}).get('node_id', task['id'])
        topic = by_topic.get(task['topic_ids'][0], {'id': task['topic_ids'][0], 'name': task.get('context', {}).get('title'), 'source_refs': task.get('context', {}).get('source_refs')})
        return (preference.get(node, len(preference)),
                source_key(topic, config.get('material_order', []), source_positions))
    template = sorted(tasks, key=priority)
    active_nodes = set(active_node_ids or [])
    fixed_ids = {t['id'] for t in template if t.get('context', {}).get('historical')
                 or t['status'] in {'completed', 'skipped', 'in_progress'}
                 or t.get('context', {}).get('node_id', t['id']) in active_nodes}
    movable = [t for t in template if t['id'] not in fixed_ids]
    return fill_ordered_slots(template, movable, fixed_ids, states), fixed_ids
