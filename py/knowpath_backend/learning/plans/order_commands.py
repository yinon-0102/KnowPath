"""Transactional commands around a shared, read-only reorder calculation."""
from copy import deepcopy
from datetime import date
from sqlalchemy import select
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.persistence.db import SessionRow
from knowpath_backend.learning.plans.ordering import material_order, source_key, source_label, canonical_id, fill_ordered_slots, ORDER_POLICY_VERSION
from knowpath_backend.learning.plans.policy import schedule_tasks


def prerequisite_conflicts(tasks, states):
    by_topic = {topic: (index, task) for index, task in enumerate(tasks)
                if not task.get('context', {}).get('historical') for topic in task['topic_ids']}
    result = []
    for index, task in enumerate(tasks):
        if task['status'] in {'completed', 'skipped', 'deferred'} or task.get('context', {}).get('historical'):
            continue
        for parent in task.get('context', {}).get('prerequisites', []):
            found = by_topic.get(parent)
            if states.get(parent, {}).get('status') == 'mastered' or (found and found[1]['status'] == 'completed'):
                continue
            if not found or found[0] >= index or found[1]['status'] in {'deferred', 'skipped'}:
                node_label = task.get('context', {}).get('title') or '、'.join(task['topic_ids'])
                prerequisite_label = (found[1].get('context', {}).get('title') if found else None) or parent
                unavailable = bool(found and found[1]['status'] in {'deferred', 'skipped'})
                message = (f"「{node_label}」依赖「{prerequisite_label}」，前置任务已延期或跳过，当前无法执行"
                           if unavailable else f"「{node_label}」必须排在前置任务「{prerequisite_label}」之后")
                result.append({'code': 'ORDER_PREREQUISITE_CONFLICT', 'message': message,
                               'node_label': node_label, 'prerequisite_label': prerequisite_label,
                               'node_id': task['node_id'], 'prerequisite_topic_id': parent,
                               'prerequisite_node_id': found[1]['node_id'] if found else None,
                               'reason': 'deferred_prerequisite' if found and found[1]['status'] == 'deferred' else 'prerequisite_after_dependent'})
    return result



def schedule_view(tasks):
    return [{'node_id': t['node_id'], 'position': t['context'].get('position'),
             'session_index': t['context'].get('session_index'), 'scheduled_date': t['context'].get('scheduled_date'),
             'estimated_minutes': t['estimated_minutes'], 'defer_until': t.get('defer_until')} for t in tasks]


class PlanOrderCommands:
    def _order_active_task_ids(self, space_id, *, lock=False):
        if not self.sql:
            active = {s["task_id"] for s in self.repository.records("sessions", space_id=space_id, lock=False) if s["status"] == "active"}
        else:
            with self.uow.session() as session:
                query = select(SessionRow).where(SessionRow.space_id == space_id, SessionRow.status == "active")
                if lock and self.uow.active:
                    query = query.with_for_update().execution_options(populate_existing=True)
                active = {s.task_id for s in session.scalars(query)}
        # Local replans copy task rows while preserving canonical node identity.
        # A real active session may still reference its base plan's task row.
        if active:
            tasks = [t for plan in self._space_plans(space_id) for t in plan["tasks"]]
            active_nodes = {t["node_id"] for t in tasks if t["id"] in active}
            active.update(t["id"] for t in tasks if t["node_id"] in active_nodes)
        return active

    def _calculate_plan_order(self, plan, payload, active_ids):
        if type(payload.get('expected_plan_version')) is not int:
            raise DomainConflict('INVALID_REQUEST', 'expected_plan_version 必填')
        if payload['expected_plan_version'] != plan['version']:
            raise DomainConflict('VERSION_CONFLICT', '计划版本已变化', {'latest_plan': plan})
        if plan['status'] != 'ready' or self._current_plan_id(plan['space_id']) != plan['id']:
            raise DomainConflict('PLAN_NOT_ACTIVE', '只能调整当前可执行计划')
        mode = payload.get('order_mode')
        if mode not in {'source', 'adaptive', 'custom'}:
            raise DomainConflict('INVALID_ORDER_MODE', 'order_mode 必须为 source、adaptive 或 custom')
        space = self.spaces.repository.get(plan['space_id'])
        candidate = deepcopy(plan)
        config = candidate['config']
        materials = material_order(space, payload.get('material_order', config.get('material_order')))
        config.update(order_mode=mode, material_order=materials, order_policy_version=ORDER_POLICY_VERSION)
        fixed, movable = [], []
        for task in candidate['tasks']:
            reason = ('active_session' if task['id'] in active_ids else 'historical' if task.get('context', {}).get('historical')
                      else task['status'] if task['status'] in {'completed', 'skipped', 'in_progress'} else None)
            if reason:
                fixed.append({'node_id': task['node_id'], 'task_id': task['id'], 'position': task['context'].get('position'), 'reason': reason})
            else:
                movable.append(task)
        node_ids = [t['node_id'] for t in movable]
        if mode == 'custom':
            requested = payload.get('ordered_node_ids')
            if not isinstance(requested, list) or any(not isinstance(i, str) for i in requested) or len(requested) != len(set(requested)) or set(requested) != set(node_ids):
                raise DomainConflict('INVALID_ORDER_PERMUTATION', 'ordered_node_ids 必须恰好包含全部可移动节点', {'movable_node_ids': node_ids})
            by_node = {t['node_id']: t for t in movable}
            ordered = [by_node[i] for i in requested]
        else:
            if payload.get('ordered_node_ids') is not None:
                raise DomainConflict('INVALID_REQUEST', '只有 custom 可以提供 ordered_node_ids')
            topics = self._ordered_topics(space, self._planning_states(space['id']), config)
            ranks = {t['id']: index for index, t in enumerate(topics)}
            ordered = sorted(movable, key=lambda t: min((ranks.get(i, 10**9) for i in t['topic_ids']), default=10**9))
        fixed_ids = {f['task_id'] for f in fixed}
        states = self._planning_states(space['id'])
        if mode == 'custom':
            iterator = iter(ordered)
            candidate['tasks'] = [t if t['id'] in fixed_ids else next(iterator) for t in candidate['tasks']]
        else:
            candidate['tasks'] = fill_ordered_slots(candidate['tasks'], ordered, fixed_ids, states)
        for position, task in enumerate(candidate['tasks']):
            task['context']['position'] = position
        conflicts = prerequisite_conflicts(candidate['tasks'], states)
        explanations = []
        topics_by_id = {t['id']: t for t in self.spaces.bound_topics(space)}
        before_positions = {t['id']: i for i, t in enumerate(plan['tasks'])}
        source_positions = self._source_positions(space)
        source_sorted = sorted(movable, key=lambda t: source_key(topics_by_id.get(t['topic_ids'][0], {'id': t['topic_ids'][0], 'name': t['context'].get('title'), 'source_refs': t['context'].get('source_refs')}), materials, source_positions))
        source_rank = {t['id']: i for i, t in enumerate(source_sorted)}
        for index, task in enumerate(candidate['tasks']):
            topic = topics_by_id.get(task['topic_ids'][0], {'id': task['topic_ids'][0], 'source_refs': task['context'].get('source_refs')})
            prerequisites = task['context'].get('prerequisites', [])
            adjusted = mode == 'source' and task['id'] not in fixed_ids and source_rank.get(task['id']) != sum(t['id'] not in fixed_ids for t in candidate['tasks'][:index])
            explanations.append({'node_id': task['node_id'], 'before_position': before_positions[task['id']], 'after_position': index,
                                 'source_label': source_label(topic, materials, source_positions), 'reason': 'fixed' if task['id'] in fixed_ids else 'prerequisite_adjustment' if adjusted else mode,
                                 'prerequisite_topic_ids': deepcopy(prerequisites)})
        if mode == 'custom':
            config['custom_node_order'] = [t['node_id'] for t in candidate['tasks'] if not t['context'].get('historical')]
            config['custom_topic_alias_order'] = [i for t in candidate['tasks'] if not t['context'].get('historical') for i in t['topic_ids']]
            config['custom_topic_order'] = list(dict.fromkeys(canonical_id(topics_by_id.get(i, {'id': i}))
                for t in candidate['tasks'] if not t['context'].get('historical') for i in t['topic_ids']))
        elif mode != 'custom':
            config['custom_topic_order'] = []
            config['custom_node_order'] = []
        adjustments = ['将前置任务移到依赖任务之前', '完成或恢复前置任务后重试', '选择来源顺序'] if conflicts else []
        conflict_code = 'ORDER_PREREQUISITE_CONFLICT' if conflicts else None
        if not conflicts:
            baseline = config.get('schedule_baseline_date') or plan['created_at'][:10]
            try:
                schedule_tasks(candidate['tasks'], config, space, date.fromisoformat(baseline), fixed_task_ids=fixed_ids)
            except DomainConflict as exc:
                conflict_code = exc.code
                conflicts = [{'code': exc.code, 'message': str(exc), **exc.details}]
                adjustments = exc.details.get('adjustable_constraints', [])
        suggested_order, suggested_plan = None, None
        suggestion_unavailable_reason, suggestion_conflicts = None, []
        if mode == 'custom' and conflict_code == 'ORDER_PREREQUISITE_CONFLICT':
            # Reuse the full source calculation, including fixed slots, original
            # schedule baseline, budget, capacity and deadline checks. The
            # requested custom candidate remains untouched and cannot apply.
            try:
                source_preview = self._calculate_plan_order(plan, {
                    'expected_plan_version': plan['version'], 'order_mode': 'source',
                    'material_order': materials}, active_ids)
                if source_preview['can_apply']:
                    suggested_order = {'expected_plan_version': plan['version'], 'order_mode': 'custom',
                                       'material_order': list(materials),
                                       'ordered_node_ids': [t['node_id'] for t in source_preview['candidate_plan']['tasks'] if t['id'] not in fixed_ids]}
                    suggested_plan = source_preview['candidate_plan']
                else:
                    suggestion_conflicts = source_preview['conflicts']
            except DomainConflict as exc:
                suggestion_conflicts = [{'code': exc.code, 'message': str(exc), **exc.details}]
            if suggested_order is None:
                suggestion_unavailable_reason = '保留固定节点与当前日程限制后，来源顺序也无法执行；需先处理前置任务或调整学习范围。'
        return {'plan_id': plan['id'], 'expected_plan_version': plan['version'], 'can_apply': not conflicts,
                'order_mode': mode, 'material_order': list(materials), 'tasks': candidate['tasks'],
                'suggested_order': suggested_order, 'suggested_plan': suggested_plan,
                'suggestion_unavailable_reason': suggestion_unavailable_reason, 'suggestion_conflicts': suggestion_conflicts,
                'candidate_plan': candidate, 'fixed_nodes': fixed, 'movable_node_ids': node_ids,
                'order_explanations': explanations, 'before_schedule': schedule_view(plan['tasks']),
                'after_schedule': schedule_view(candidate['tasks']), 'conflicts': conflicts,
                'conflict_code': conflict_code, 'adjustable_constraints': adjustments}

    def preview_plan_order(self, plan_id, payload):
        plan = self.get_plan(plan_id)
        # No command wrapper: preview has no replay records, runs or writes.
        active_ids = self._order_active_task_ids(plan['space_id'])
        return self._calculate_plan_order(plan, dict(payload or {}), active_ids)

    def update_plan_order(self, plan_id, payload, idempotency_key=None):
        payload = dict(payload or {})
        def change():
            plan = self.get_plan(plan_id)
            self.spaces.repository.get(plan['space_id'])
            if self.sql:
                row, task_rows = self._get_plan_sql(plan_id, lock=True)
                plan = self.get_plan(plan_id)
            active_ids = self._order_active_task_ids(plan['space_id'], lock=True)
            preview = self._calculate_plan_order(plan, payload, active_ids)
            if not preview['can_apply']:
                raise DomainConflict(preview['conflict_code'], '重排与前置关系或日程约束冲突',
                                     {'conflicts': preview['conflicts'], 'adjustable_constraints': preview['adjustable_constraints'],
                                      'fixed_nodes': preview['fixed_nodes'],
                                      'suggested_order': preview['suggested_order'], 'suggested_plan': preview['suggested_plan'],
                                      'suggestion_unavailable_reason': preview['suggestion_unavailable_reason'],
                                      'suggestion_conflicts': preview['suggestion_conflicts']})
            candidate = preview['candidate_plan']
            candidate['version'] = plan['version'] + 1
            if self.sql:
                row.config = deepcopy(candidate['config'])
                row.version = candidate['version']
                by_id = {t['id']: t for t in candidate['tasks']}
                for task in task_rows:
                    task.context = deepcopy(by_id[task.id]['context'])
            else:
                self.repository.materials.assessment_data['plans'][plan_id] = deepcopy(candidate)
                self.memory_plans[plan_id] = deepcopy(candidate)
            return candidate
        return self.assessments._execute('plan.order', plan_id, payload, idempotency_key, change)
