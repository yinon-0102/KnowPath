"""Versioned in-place plan changes and durable, model-free refresh jobs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .rolling import allocate_ordered_tasks, local_day
from .policy import schedule_tasks
from .comparison_schemas import ComparePlans
from ..errors import DomainConflict, DomainNotFound


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode()).hexdigest()


def uid():
    return str(uuid4())


class PlanAdjustmentService:
    def __init__(self, plans):
        self.plans = plans
        self.repository, self.spaces, self.runs = plans.repository, plans.spaces, plans.runs

    def latest(self, plan_id):
        rows = self.repository.records('plan_adjustments', plan_id=plan_id, lock=False)
        if not rows:
            return None
        row = rows[-1]
        return {k: deepcopy(row.get(k)) for k in ('id', 'status', 'run_id', 'trigger', 'changed_count', 'error', 'created_at', 'before_version', 'after_version')}

    def history(self, plan_id):
        self.plans.get_plan(plan_id)
        return {'plan_id': plan_id, 'items': self.repository.records('plan_adjustments', plan_id=plan_id, lock=False)}

    def _basis(self, plan, space):
        sessions = self.plans._space_sessions(space['id'])
        assessments = self.repository.records('assessments', space_id=space['id'])
        return {'plan_id': plan['id'], 'plan_version': plan['version'], 'scope_version': space['scope_version'],
                'profile_version': space['profile_version'], 'state_version': space['state_version'],
                'bindings': space['bindings'], 'tasks': plan['tasks'], 'config': plan['config'],
                'session_basis': [(s['id'], s['status'], s.get('context', {}).get('activity')) for s in sessions],
                'assessment_basis': [(a['id'], a['status'], a.get('snapshot', {}).get('answer_activity', {})) for a in assessments],
                'calendar_day': local_day(now(), plan['config']).isoformat()}

    def _raw(self, plan_id):
        if self.plans.sql:
            return self.plans._plan_payload(*self.plans._get_plan_sql(plan_id, lock=True))
        row = self.plans._load_plan_memory(plan_id)
        if row is None:
            raise DomainNotFound('plan', plan_id)
        return self.plans._plan_payload(row, row['tasks'])

    def _publish(self, plan, space, *, trigger, run_id=None, record_id=None):
        before = self._raw(plan['id'])
        plan['version'] = before['version'] + 1
        plan['config']['snapshot'] = self.plans._snapshot(space, self.plans._planning_states(space['id']))
        plan['config']['next_refresh_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        if self.plans.sql:
            row, tasks = self.plans._get_plan_sql(plan['id'], lock=True)
            row.config = deepcopy(plan['config'])
            row.version = plan['version']
            by_id = {t['id']: t for t in plan['tasks']}
            for task in tasks:
                item = by_id[task.id]
                task.context, task.kind = deepcopy(item['context']), item['kind']
                task.estimated_minutes, task.reason = item['estimated_minutes'], item['reason']
        else:
            self.repository.materials.assessment_data['plans'][plan['id']] = deepcopy(plan)
            self.plans.memory_plans[plan['id']] = deepcopy(plan)
        identifier = record_id or uid()
        before_by_id = {t['id']: t for t in before['tasks']}
        changes = [{'task_id': t['id'], 'node_id': t['node_id'], 'before': before_by_id[t['id']], 'after': deepcopy(t)}
                   for t in plan['tasks'] if t != before_by_id[t['id']]]
        existing = self.repository.get_record('plan_adjustments', identifier) if record_id else {}
        record = {**existing, 'id': identifier, 'space_id': space['id'], 'plan_id': plan['id'], 'status': 'applied',
            'run_id': run_id, 'trigger': trigger, 'before_version': before['version'], 'after_version': plan['version'],
            'state_version': space['state_version'], 'scope_version': space['scope_version'],
            'changes': changes, 'changed_count': len(changes), 'before_config': before['config'],
            'after_config': deepcopy(plan['config']), 'created_at': existing.get('created_at', now()), 'error': None}
        self.repository.put_record('plan_adjustments', record)
        plan['adjustment'] = self.latest(plan['id'])
        return plan

    def compare(self, space_id, payload, key=None):
        request = ComparePlans.model_validate(payload).model_dump()
        def change():
            space = self.spaces.repository.get(space_id)
            plan_id = self.plans._current_plan_id(space_id)
            if not plan_id:
                raise DomainConflict('NO_CURRENT_PLAN', '请先建立学习计划，再比较可应用的预算')
            plan = self._raw(plan_id)
            if self.plans.get_plan(plan_id)['status'] not in {'ready'}:
                raise DomainConflict('STALE_LEARNING_CONTEXT', '请先更新计划的资料与范围')
            protected = self.plans.protected_task_ids(space_id)
            tasks = self.plans.forecast_tasks(deepcopy(plan['tasks']), space, protected_ids=protected)
            self.plans.accumulate_tasks(tasks, space_id)
            identifier, scenarios = uid(), []
            basis = self._basis(plan, space)
            for budget in request['budgets_minutes_per_day']:
                config = {**plan['config'], 'schedule_mode': 'budget_rolling', 'daily_minutes': budget,
                    'horizon_days': request['horizon_days'], 'calendar_timezone': plan['config'].get('calendar_timezone', 'Asia/Shanghai')}
                result = allocate_ordered_tasks(tasks, config, space, local_day(now(), config),
                    spent_minutes=self.plans.spent_minutes(space_id, config), protected_ids=self.plans._order_active_task_ids(space_id),
                    consumed_by_task=self.plans.task_elapsed(tasks, space_id))
                scenario_id = uid()
                scenarios.append({**result, 'scenario_id': scenario_id, 'daily_minutes': budget, 'config': config,
                    'coverage_count': sum(t.get('context', {}).get('allocation_state') == 'scheduled' for t in result['tasks']),
                    'total_count': len(tasks), 'can_apply': True,
                    'assumptions': ['时间是估计，分配不代表掌握；完整范围与节点顺序保持不变。']})
            snapshot = {'hash': digest(basis), 'plan_id': plan_id, 'plan_version': plan['version'],
                        'state_version': space['state_version'], 'calendar_day': basis['calendar_day']}
            record = {'id': identifier, 'space_id': space_id, 'plan_id': plan_id, 'status': 'ready', 'run_id': None,
                'snapshot': snapshot, 'request': request, 'scenarios': scenarios, 'created_at': now()}
            self.repository.put_record('budget_comparisons', record)
            return {'comparison_id': identifier, 'space_id': space_id, 'snapshot': snapshot, 'scenarios': scenarios,
                    'strategy_version': 'budget-ordered-rolling-v1', 'created_at': record['created_at']}
        return self.plans.assessments._execute('budget.compare', space_id, request, key, change)

    def apply_schedule(self, plan_id, payload, key=None):
        def change():
            candidate = self.repository.get_record('budget_comparisons', payload['comparison_id'], lock=False)
            space = self.spaces.repository.get(candidate['space_id'])
            plan = self._raw(plan_id)
            if candidate['plan_id'] != plan_id or plan['space_id'] != space['id']:
                raise DomainConflict('PLAN_SPACE_MISMATCH', '预算方案不属于当前计划')
            self._check_current(plan, space, payload.get('expected_plan_version'))
            snapshot_hash = digest(self._basis(plan, space))
            if candidate['snapshot']['hash'] != snapshot_hash or payload.get('expected_snapshot_hash') != snapshot_hash:
                raise DomainConflict('VERSION_CONFLICT', '比较快照已变化，请重新预览')
            scenario = next((s for s in candidate['scenarios'] if s['scenario_id'] == payload['scenario_id']), None)
            if not scenario or candidate['status'] != 'ready':
                raise DomainConflict('INVALID_SCENARIO', '预算方案无效或已失效')
            plan['config'] = deepcopy(scenario['config'])
            plan['tasks'] = deepcopy(scenario['tasks'])
            result = self._publish(plan, space, trigger='budget_applied')
            return result
        return self.plans.assessments._execute('plan.schedule', plan_id, payload, key, change)

    def _check_current(self, plan, space, expected=None):
        if expected is not None and expected != plan['version']:
            raise DomainConflict('VERSION_CONFLICT', '计划版本已变化')
        if space.get('status') == 'archived' or self.plans._current_plan_id(space['id']) != plan['id'] or plan['status'] == 'superseded':
            raise DomainConflict('PLAN_NOT_ACTIVE', '计划不再可修改')
        saved = plan['config'].get('snapshot', {})
        current = self.plans._snapshot(space, self.plans._planning_states(space['id']))
        if any(saved.get(k) != current[k] for k in ('scope_version', 'bindings', 'policy_version')):
            raise DomainConflict('STALE_LEARNING_CONTEXT', '资料或范围已变化，需要显式更新计划')

    def enqueue(self, space_id, trigger='manual'):
        with self.repository.transaction():
            space = self.spaces.repository.get(space_id)
            prefs = space.get('profile', {}).get('preferences', {}).get('value') or {}
            if space.get('status') == 'archived' or not prefs.get('automatic_adjustment', True):
                return None
            plan_id = self.plans._current_plan_id(space_id)
            if not plan_id:
                return None
            active = [r for r in self.repository.records('plan_adjustments', plan_id=plan_id, lock=False) if r['status'] == 'queued']
            if active:
                return {k: active[-1][k] for k in ('id', 'run_id', 'event_id', 'status')}
            run = self.runs.create('plan_adjustment')
            identifier, event_id = uid(), uid()
            self.repository.put_record('plan_adjustments', {'id': identifier, 'space_id': space_id, 'plan_id': plan_id,
                'status': 'queued', 'run_id': run['id'], 'event_id': event_id, 'trigger': trigger, 'created_at': now()})
            self.repository.put_record('outbox', {'id': event_id, 'event_type': 'plan.refresh_requested',
                'aggregate_type': 'plan', 'aggregate_id': plan_id,
                'payload': {'space_id': space_id, 'run_id': run['id'], 'adjustment_id': identifier},
                'status': 'pending', 'attempts': 0, 'lease_token': None, 'lease_until': None,
                'available_at': None, 'created_at': now()})
            return {'id': identifier, 'run_id': run['id'], 'event_id': event_id, 'status': 'queued'}

    def refresh(self, plan_id, payload, key=None):
        def change():
            plan = self.plans.get_plan(plan_id)
            self._check_current(plan, self.spaces.repository.get(plan['space_id']), payload.get('expected_plan_version'))
            return self.enqueue(plan['space_id'], 'manual') or {'status': 'disabled', 'plan_id': plan_id}
        return self.plans.assessments._execute('plan.refresh', plan_id, payload, key, change)

    def run_once(self, event_id=None):
        candidates = [r for r in self.repository.records('outbox', lock=False)
            if r['event_type'] == 'plan.refresh_requested' and r['status'] == 'pending' and (not event_id or r['id'] == event_id)]
        if not candidates:
            return False
        candidate = candidates[0]
        with self.repository.transaction():
            space = self.spaces.repository.get(candidate['payload']['space_id'])
            plan = self._raw(candidate['aggregate_id'])
            event = self.repository.get_record('outbox', candidate['id'])
            if event['status'] != 'pending':
                return False
            job = self.repository.get_record('plan_adjustments', event['payload']['adjustment_id'])
            run = self.runs.get(job['run_id'])
            if run['status'] in {'cancelling', 'cancelled'}:
                self.runs.acknowledge_cancel(run['id'])
                job['status'], event['status'] = 'cancelled', 'cancelled'
            else:
                if run['status'] == 'queued':
                    self.runs.start(run['id'])
                try:
                    prefs = space.get('profile', {}).get('preferences', {}).get('value') or {}
                    if not prefs.get('automatic_adjustment', True):
                        raise DomainConflict('AUTOMATIC_ADJUSTMENT_DISABLED', '自动调整已关闭，已排任务不再发布')
                    self._check_current(plan, space)
                    protected = self.plans.protected_task_ids(space['id'])
                    states = self.plans._planning_states(space['id'])
                    original = deepcopy(plan)
                    for task in plan['tasks']:
                        if task['status'] == 'deferred' and task.get('defer_until') and datetime.fromisoformat(task['defer_until']) <= datetime.fromisoformat(now()):
                            task['context']['previous_defer_until'] = task['defer_until']
                            task.update(status='pending', defer_until=None)
                            protected.discard(task['id'])
                        if task['id'] in protected:
                            continue
                        values = [states.get(t, {}) for t in task['topic_ids']]
                        context = task.setdefault('context', {})
                        context['not_required'] = bool(values and all(s.get('status') == 'mastered' and s.get('score_validity') == 'current' and not s.get('review_due') for s in values))
                        if any(s.get('score_validity') == 'stale' for s in values):
                            task['kind'], task['reason'] = 'diagnostic', '知识依据需要重新验证'
                        elif any(s.get('review_due') or s.get('status') == 'needs_review' for s in values):
                            task['kind'], task['reason'] = 'review', '当前独立证据对应的复习已到期'
                        elif any(s.get('score_validity') == 'current' and s.get('independent_evidence_count', 0) > 0 and (s.get('error_tags') or s.get('mastery_score') is not None and s['mastery_score'] < .6) for s in values):
                            task['kind'], task['reason'] = 'targeted_practice', '观察到有效作答错误，安排巩固练习'
                        else:
                            task['kind'], task['reason'] = 'learn', '当前继续来源学习；未知证据不作为能力不足'
                    self.plans.forecast_tasks(plan['tasks'], space, states, protected_ids=protected)
                    self.plans.accumulate_tasks(plan['tasks'], space['id'])
                    if plan['config'].get('schedule_mode') == 'budget_rolling':
                        allocation = allocate_ordered_tasks(plan['tasks'], plan['config'], space, local_day(now(), plan['config']),
                            spent_minutes=self.plans.spent_minutes(space['id'], plan['config']),
                            protected_ids=self.plans._order_active_task_ids(space['id']),
                            consumed_by_task=self.plans.task_elapsed(plan['tasks'], space['id']))
                        plan['tasks'] = allocation['tasks']
                    else:
                        schedule_tasks(plan['tasks'], plan['config'], space, datetime.now(timezone.utc).date(), fixed_task_ids=protected)
                    if plan['tasks'] != original['tasks'] or plan['config'].get('snapshot') != self.plans._snapshot(space, states):
                        self._publish(plan, space, trigger=job['trigger'], run_id=run['id'], record_id=job['id'])
                        job = self.repository.get_record('plan_adjustments', job['id'])
                    else:
                        job.update(status='unchanged', changed_count=0)
                        plan['config']['next_refresh_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                        if self.plans.sql:
                            row, _ = self.plans._get_plan_sql(plan['id'], lock=True)
                            row.config = deepcopy(plan['config'])
                        else:
                            self.repository.materials.assessment_data['plans'][plan['id']] = deepcopy(plan)
                            self.plans.memory_plans[plan['id']] = deepcopy(plan)
                    self.runs.complete(run['id'], {'type': 'plan_adjustment', 'id': job['id']})
                    event['status'] = 'completed'
                except DomainConflict as exc:
                    job.update(status='blocked', error={'code': exc.code, 'message': str(exc), 'details': exc.details})
                    self.runs.fail(run['id'], job['error'])
                    event['status'] = 'completed'
            self.repository.put_record('plan_adjustments', job)
            self.repository.put_record('outbox', event)
        return True

    def tick(self):
        for raw in self.repository.records('plans', lock=False):
            if raw['status'] == 'superseded':
                continue
            plan = self.plans.get_plan(raw['id'])
            due = plan['config'].get('next_refresh_at')
            if plan['status'] == 'ready' and (plan.get('adjustment_pending') or (due and due <= now()) or not due and plan['config'].get('schedule_mode') == 'budget_rolling'):
                self.enqueue(plan['space_id'], 'calendar_or_state')

    def revert(self, identifier, payload, key=None):
        def change():
            record = self.repository.get_record('plan_adjustments', identifier, lock=False)
            space = self.spaces.repository.get(record['space_id'])
            plan = self._raw(record['plan_id'])
            self._check_current(plan, space, payload.get('expected_plan_version'))
            if record['status'] != 'applied' or record.get('after_version') != plan['version'] or record.get('state_version') != space['state_version']:
                raise DomainConflict('VERSION_CONFLICT', '后续安排或学习证据已变化，请重新预览')
            protected = self.plans.protected_task_ids(space['id'])
            changes = [c for c in record.get('changes', []) if c['task_id'] not in protected]
            before = {c['task_id']: c['before'] for c in changes}
            candidate = deepcopy(plan)
            candidate['tasks'] = [deepcopy(before.get(t['id'], t)) for t in plan['tasks']]
            candidate['config'] = deepcopy(record['before_config'])
            if candidate['config'].get('schedule_mode') == 'budget_rolling':
                candidate['tasks'] = allocate_ordered_tasks(candidate['tasks'], candidate['config'], space, local_day(now(), candidate['config']),
                    spent_minutes=self.plans.spent_minutes(space['id'], candidate['config']), protected_ids=protected,
                    consumed_by_task=self.plans.task_elapsed(candidate['tasks'], space['id']))['tasks']
            else:
                schedule_tasks(candidate['tasks'], candidate['config'], space, datetime.now(timezone.utc).date(), fixed_task_ids=protected)
            if not payload.get('apply', False):
                current = {t['id']: t for t in plan['tasks']}
                preview_changes = [{'task_id': t['id'], 'node_id': t['node_id'], 'before': current[t['id']], 'after': deepcopy(t)} for t in candidate['tasks'] if t != current[t['id']]]
                return {'can_apply': True, 'plan_id': plan['id'], 'expected_plan_version': plan['version'], 'changes': preview_changes}
            result = self._publish(candidate, space, trigger='user_reverted')
            record['status'] = 'reverted'
            self.repository.put_record('plan_adjustments', record)
            return result
        return self.plans.assessments._execute('plan.revert', identifier, payload, key, change)
