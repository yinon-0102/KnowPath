"""Small, read-only completion receipts and continuation projections."""
from __future__ import annotations

import json
import logging
from hashlib import sha256

from sqlalchemy import select

from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.learning.persistence.db import (
    AssessmentRow, LearningSpaceRow, RunRow, SessionRow, StudyPlanRow, StudyTaskRow,
)

logger = logging.getLogger(__name__)


class TaskCompletionReceipts:
    @staticmethod
    def _completion_receipt(result, space_id):
        completion = result.get('context', {}).get('completion')
        if result.get('status') != 'completed' or not completion:
            return result
        return result | {'space_id': space_id, 'completion_receipt': {
            'space_id': space_id, 'plan_id': result['plan_id'], 'task_id': result['id'],
            'plan_version': result['plan_version'], 'event_id': completion['event_id'], 'source': completion['source'],
            **({'assessment_id': completion['assessment_id']} if completion.get('assessment_id') else {})}}

    def _completion_context(self, plan_id):
        """No write transaction, history aggregation, answer reads or model calls."""
        if self.sql:
            with self.uow.session() as session:
                requested = session.get(StudyPlanRow, plan_id)
                if requested is None:
                    raise DomainNotFound('plan', plan_id)
                space_row = session.get(LearningSpaceRow, requested.space_id)
                if space_row is None:
                    raise DomainNotFound('learning_space', requested.space_id)
                space = self.spaces.repository._read(space_row)
                current = session.scalar(select(StudyPlanRow)
                    .where(StudyPlanRow.space_id == requested.space_id, StudyPlanRow.status != 'superseded')
                    .order_by(StudyPlanRow.created_at.desc(), StudyPlanRow.id.desc()).limit(1))
                task_rows = list(session.scalars(select(StudyTaskRow)
                    .where(StudyTaskRow.plan_id == plan_id)))
                task_rows.sort(key=lambda row: ((row.context or {}).get('position', 10**9), row.id))
                plan = self._plan_payload(requested, task_rows)
                active = session.scalar(select(SessionRow).where(
                    SessionRow.space_id == requested.space_id, SessionRow.status == 'active')
                    .order_by(SessionRow.started_at.desc(), SessionRow.id.desc()).limit(1))
                active = {'id': active.id, 'session_id': active.id, 'space_id': active.space_id,
                    'plan_id': active.plan_id, 'task_id': active.task_id, 'status': active.status,
                    'context': self._record_context(active.context or {})} if active else None
                # Only resume identities/statuses are needed, never frozen questions,
                # attempt histories or feedback. Failed generation runs are excluded.
                rows = session.execute(select(AssessmentRow.id, AssessmentRow.context,
                    AssessmentRow.status, RunRow.status.label('run_status'))
                    .outerjoin(RunRow, RunRow.id == AssessmentRow.run_id)
                    .where(AssessmentRow.space_id == requested.space_id,
                           AssessmentRow.status.in_(['generating', 'ready', 'in_progress']))).all()
                recoverable = [{'assessment_id': row.id, 'plan_id': (row.context or {}).get('plan_id'),
                    'task_id': (row.context or {}).get('task_id')} for row in rows
                    if row.status != 'generating' or row.run_status not in {'failed', 'cancelled'}]
                return space, plan, current.id if current else None, active, recoverable
        with self.repository.materials._lock:
            raw = self._load_plan_memory(plan_id)
            if raw is None:
                raise DomainNotFound('plan', plan_id)
            space = self.spaces.repository.get(raw['space_id'])
            plan = self._plan_payload(raw, raw['tasks'])
            candidates = [row for row in self.repository.records('plans', space_id=space['id'], lock=False)
                          if row['status'] != 'superseded']
            current = max(candidates, key=self._plan_key, default=None)
            sessions = self.repository.records('sessions', space_id=space['id'], status='active', lock=False)
            active = max(sessions, key=lambda row: (row.get('started_at', ''), row['id']), default=None)
            active = {'id': active['id'], 'session_id': active['id'], 'space_id': space['id'],
                'plan_id': active['plan_id'], 'task_id': active['task_id'], 'status': active['status'],
                'context': self._record_context(active.get('context', {}))} if active else None
            recoverable = []
            for row in self.repository.records('assessments', space_id=space['id'], lock=False):
                if row['status'] not in {'generating', 'ready', 'in_progress'}:
                    continue
                if row['status'] == 'generating' and self.runs.get(row['run_id'])['status'] in {'failed', 'cancelled'}:
                    continue
                recoverable.append({'assessment_id': row['id'], 'plan_id': row.get('plan_id'), 'task_id': row.get('task_id')})
            return space, plan, current['id'] if current else None, active, recoverable

    @staticmethod
    def _assessment_continuation(rows):
        if len(rows) == 1:
            return {'kind': 'assessment', **{key: value for key, value in rows[0].items() if value}}
        return {'kind': 'assessment', **{key: rows[0][key] for key in ('plan_id', 'task_id')
            if rows[0].get(key) and all(row.get(key) == rows[0][key] for row in rows)}}

    def _completion_continuation(self, result):
        space, plan, current_id, active, recovery = self._completion_context(result['plan_id'])
        task = next((row for row in plan['tasks'] if row['id'] == result['id']), None)
        receipt = result.get('completion_receipt') or {}
        completion = (task or {}).get('context', {}).get('completion') or {}
        valid = (current_id == plan['id'] and plan['status'] != 'superseded' and task is not None
            and task['status'] == 'completed' and not task.get('historical')
            and not task.get('context', {}).get('historical')
            and receipt.get('space_id') == space['id'] and receipt.get('plan_id') == plan['id']
            and receipt.get('task_id') == task['id'] and receipt.get('event_id') == completion.get('event_id')
            and receipt.get('assessment_id') == completion.get('assessment_id')
            and plan['version'] >= receipt.get('plan_version', plan['version'] + 1))
        if not valid:
            return result | {'completion_state': 'superseded', 'next_action': None, 'navigation_pending': False}
        tasks = [row for row in plan['tasks'] if not row.get('historical') and not row.get('context', {}).get('historical')]
        pending_ids = {row['id'] for row in tasks if row['status'] in {'pending', 'in_progress'}}
        linked = [row for row in recovery if row.get('plan_id') == plan['id'] and row.get('task_id') in pending_ids]
        free = [row for row in recovery if not row.get('plan_id') and not row.get('task_id')]
        saved = plan.get('config', {}).get('snapshot') or {}
        needs_replan = (plan['status'] == 'needs_replan' or not saved or
            saved.get('scope_version') != space['scope_version'] or saved.get('bindings') != space['bindings']
            or saved.get('policy_version') != self.policy.version)
        if space['status'] == 'archived':
            action = {'kind': 'archived'}
        elif not space.get('topic_ids'):
            action = {'kind': 'scope'}
        elif active:
            action = {'kind': 'session', **{key: active[key] for key in ('session_id', 'plan_id', 'task_id')}}
        elif linked or free:
            action = self._assessment_continuation(linked or free)
        elif needs_replan:
            action = {'kind': 'replan', 'plan_id': plan['id']}
        else:
            action = self._task_progress(plan)['next_action']
        if needs_replan:
            plan['status'] = 'needs_replan'
        counts = self._task_progress(plan)['task_progress']
        summary = {'plan_id': plan['id'], 'plan_version': plan['version'], 'tasks': counts,
            'processed': counts['remaining'] == 0,
            'all_completed': bool(counts['total']) and counts['completed'] == counts['total'],
            'details_pending': True}
        return result | {'completion_state': 'confirmed', 'current_plan_version': plan['version'],
            'next_action': action, 'navigation_pending': False, 'continuation_context': {
                'space_id': space['id'], 'current_plan': plan, 'active_session': active, 'round_summary': summary}}

    def _with_completion_navigation(self, result):
        if not result.get('completion_receipt'):
            return result
        try:
            return self._completion_continuation(result)
        except Exception:
            # The write is already committed. Never report an auxiliary read as a
            # failed completion, or discard the identity needed for recovery.
            logger.warning('TASK_COMPLETION_NAVIGATION_PENDING', exc_info=True)
            return result | {'next_action': None, 'navigation_pending': True}

    def task_completion_status(self, plan_id, task_id, payload, idempotency_key=None):
        if not idempotency_key:
            raise DomainConflict('IDEMPOTENCY_KEY_REQUIRED', '查询保存结果必须提供原 Idempotency-Key')
        payload = dict(payload or {})
        payload.setdefault('source', 'manual')
        if payload.get('status') != 'completed':
            raise DomainConflict('INVALID_TASK_STATUS', '只能查询任务完成确认结果')
        fingerprint = sha256(json.dumps(['plan.task.update', plan_id, {**payload, 'task_id': task_id}],
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        original = self.repository.replay(idempotency_key, fingerprint)
        if original is None:
            return {'status': 'unknown'}
        # Older receipts are upgraded from their original stored event, never
        # inferred from the current task merely having status completed.
        if not original.get('completion_receipt'):
            space, _, _, _, _ = self._completion_context(plan_id)
            original = self._completion_receipt(original, space['id'])
        if not original.get('completion_receipt'):
            return {'status': 'superseded', 'message': '原请求没有可核实的完成事件，请重新核对任务。'}
        # A query may only say confirmed after checking the *current* event.
        # If that read fails, return the ordinary retryable HTTP failure instead
        # of presenting an old committed receipt as a current confirmation.
        result = self._completion_continuation(original)
        if result.get('completion_state') == 'superseded':
            return {'status': 'superseded', 'message': '原完成事件已撤销、被替代或属于历史计划，请重新核对。'}
        return {'status': 'confirmed', 'result': result}
