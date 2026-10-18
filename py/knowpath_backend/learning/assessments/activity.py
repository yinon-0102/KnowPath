"""Durable question exposure accounting using server receipt times."""
from copy import deepcopy

from ..plans.activity import new_activity, advance_activity
from ..errors import DomainConflict


class AssessmentActivity:
    def _session_phase(self, assessment, phase_id, event, timestamp, *, claim=False):
        session_id = assessment.get('learning_session_id')
        plans = getattr(self, 'plan_sessions', None)
        if not session_id or not plans:
            return
        # Callers own assessment -> space before taking the session lock,
        # matching finalization. Never lock another assessment here.
        row = plans._find_session_sql(session_id, lock=True) if plans.sql else plans._load_session_memory(session_id)
        if not row:
            return
        field = lambda name: getattr(row, name) if plans.sql else row.get(name)
        if (field('space_id') != assessment['space_id']
                or any(assessment.get(name) and field(name) != assessment[name] for name in ('plan_id', 'task_id'))):
            raise DomainConflict('SESSION_TASK_MISMATCH', '测评与封存学习会话归属不一致')
        if field('status') != 'active':
            return
        context = deepcopy(field('context') or {})
        owner = {'assessment_id': assessment['id'], 'phase_id': phase_id}
        current = context.get('assessment_phase')
        if claim:
            context['assessment_phase'] = owner
        elif current != owner:
            # Legacy owners may settle unowned sessions, never a newer owner.
            if current or not phase_id.startswith('legacy:'):
                return
        context['activity'] = advance_activity(context.get('activity'), timestamp, event)
        context['paused'] = bool(context['activity']['pause_reasons'])
        if plans.sql:
            row.context = context
        else:
            row['context'] = context

    def activity(self, assessment_id, payload, key=None):
        from .service import now
        def change():
            assessment = self.repository.get_record('assessments', assessment_id)
            self.spaces.repository.get(assessment['space_id'])
            question_id, event = payload.get('question_id'), payload.get('event', 'heartbeat')
            if event not in {'shown', 'heartbeat', 'pause', 'resume', 'hidden', 'visible', 'disconnect', 'reconnect', 'waiting', 'ready', 'assessment_end'}:
                raise DomainConflict('INVALID_REQUEST', '作答活动事件无效')
            phase_id = payload.get('phase_id') or ('legacy:' + question_id if question_id else None)
            if not phase_id:
                raise DomainConflict('INVALID_REQUEST', '测评阶段缺少身份')
            snapshot = assessment['snapshot']
            phases = snapshot.setdefault('activity_phases', {})
            activities = snapshot.setdefault('answer_activity', {})
            phase, timestamp = phases.get(phase_id), now()
            if event == 'assessment_end':
                # An end remains valid after the exposed question is submitted.
                # Unknown tokens are tombstoned, fencing a delayed shown request.
                if phase is None:
                    if not payload.get('phase_id') and question_id not in activities:
                        raise DomainConflict('QUESTION_NOT_CURRENT', '未知历史题目活动')
                    phase = phases[phase_id] = {'question_id': question_id, 'ended': False}
                question_id = phase['question_id']
                if not phase['ended']:
                    if snapshot.get('active_phase_id') in {None, phase_id} and question_id in activities:
                        activities[question_id] = advance_activity(activities[question_id], timestamp, 'hidden')
                    phase['ended'] = True
                    self._session_phase(assessment, phase_id, 'assessment_end', timestamp)
                self.repository.put_record('assessments', assessment)
                return {'assessment_id': assessment_id, 'phase_id': phase_id, 'question_id': question_id,
                        'activity': deepcopy(activities.get(question_id))}
            if phase and phase['ended']:
                return {'assessment_id': assessment_id, 'phase_id': phase_id, 'question_id': phase['question_id'],
                        'activity': deepcopy(activities.get(phase['question_id']))}
            if assessment['status'] not in {'ready', 'in_progress'}:
                raise DomainConflict('ASSESSMENT_NOT_READY', '当前测评不接受活动计时')
            questions = {q['id'] for q in assessment['questions']}
            latest = self._latest(assessment_id)
            diagnostic = assessment['snapshot'].get('adaptive')
            if question_id not in questions or question_id in latest or diagnostic and diagnostic['current_question_id'] != question_id:
                raise DomainConflict('QUESTION_NOT_CURRENT', '只为当前未提交问题记录活动')
            if phase and phase['question_id'] != question_id:
                raise DomainConflict('INVALID_REQUEST', '测评阶段题目身份不一致')
            new_phase = phase is None
            if new_phase:
                if payload.get('phase_id') and event != 'shown':
                    raise DomainConflict('INVALID_REQUEST', '测评阶段必须先展示题目')
                previous = snapshot.get('active_phase_id')
                if previous in phases:
                    phases[previous]['ended'] = True
                phases[phase_id] = {'question_id': question_id, 'ended': False}
                snapshot['active_phase_id'] = phase_id
            for identifier, activity in list(activities.items()):
                if identifier != question_id and activity.get('status') == 'active':
                    activities[identifier] = advance_activity(activity, timestamp, 'hidden')
            current = activities.get(question_id)
            if current is None:
                current = new_activity(timestamp)
            activities[question_id] = advance_activity(current, timestamp, 'visible' if event == 'shown' else event)
            reasons = activities[question_id]['pause_reasons']
            phase_event = 'assessment_end' if 'hidden' in reasons or 'disconnected' in reasons else 'assessment_start'
            self._session_phase(assessment, phase_id, phase_event, timestamp, claim=new_phase)
            self.repository.put_record('assessments', assessment)
            return {'assessment_id': assessment_id, 'phase_id': phase_id, 'question_id': question_id, 'activity': deepcopy(activities[question_id])}
        return self._execute('assessment.activity', assessment_id, payload, key, change)

    def finish_question_activity(self, assessment, question_id):
        from .service import now
        activities = assessment['snapshot'].get('answer_activity', {})
        if question_id in activities:
            activities[question_id] = advance_activity(activities[question_id], now(), 'finish')

    def finish_assessment_activity(self, assessment):
        from .service import now
        for identifier in list(assessment['snapshot'].get('answer_activity', {})):
            self.finish_question_activity(assessment, identifier)
        snapshot = assessment['snapshot']
        phase_id = snapshot.get('active_phase_id')
        if phase_id:
            snapshot['activity_phases'][phase_id]['ended'] = True
        self._session_phase(assessment, phase_id or 'legacy:finish', 'assessment_end', now())
