"""Durable question exposure accounting using server receipt times."""
from copy import deepcopy

from ..plans.activity import new_activity, advance_activity
from ..errors import DomainConflict


class AssessmentActivity:
    def activity(self, assessment_id, payload, key=None):
        from .service import now
        def change():
            assessment = self.repository.get_record('assessments', assessment_id)
            self.spaces.repository.get(assessment['space_id'])
            if assessment['status'] not in {'ready', 'in_progress'}:
                raise DomainConflict('ASSESSMENT_NOT_READY', '当前测评不接受活动计时')
            question_id, event = payload['question_id'], payload.get('event', 'heartbeat')
            if event not in {'shown', 'heartbeat', 'pause', 'resume', 'hidden', 'visible', 'disconnect', 'reconnect', 'waiting', 'ready'}:
                raise DomainConflict('INVALID_REQUEST', '作答活动事件无效')
            questions = {q['id'] for q in assessment['questions']}
            latest = self._latest(assessment_id)
            diagnostic = assessment['snapshot'].get('adaptive')
            if question_id not in questions or question_id in latest or diagnostic and diagnostic['current_question_id'] != question_id:
                raise DomainConflict('QUESTION_NOT_CURRENT', '只为当前未提交问题记录活动')
            snapshot = assessment['snapshot']
            activities = snapshot.setdefault('answer_activity', {})
            for identifier, activity in list(activities.items()):
                if identifier != question_id and activity.get('status') == 'active':
                    activities[identifier] = advance_activity(activity, now(), 'hidden')
            current = activities.get(question_id)
            if current is None:
                current = new_activity(now())
            activities[question_id] = advance_activity(current, now(), 'visible' if event == 'shown' else event)
            session_id = assessment.get('learning_session_id')
            if session_id and getattr(self, 'plan_sessions', None):
                plans = self.plan_sessions
                row = plans._find_session_sql(session_id, lock=True) if plans.sql else plans._load_session_memory(session_id)
                if row:
                    status = row.status if plans.sql else row['status']
                    if status == 'active':
                        context = deepcopy(row.context if plans.sql else row.get('context', {}))
                        reasons = activities[question_id]['pause_reasons']
                        phase_event = 'assessment_end' if 'hidden' in reasons or 'disconnected' in reasons else 'assessment_start'
                        context['activity'] = advance_activity(context.get('activity'), now(), phase_event)
                        if plans.sql:
                            row.context = context
                        else:
                            row['context'] = context
            self.repository.put_record('assessments', assessment)
            return {'assessment_id': assessment_id, 'question_id': question_id, 'activity': deepcopy(activities[question_id])}
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
        session_id = assessment.get('learning_session_id')
        if session_id and getattr(self, 'plan_sessions', None):
            plans = self.plan_sessions
            row = plans._find_session_sql(session_id, lock=True) if plans.sql else plans._load_session_memory(session_id)
            if row:
                context = deepcopy(row.context if plans.sql else row.get('context', {}))
                context['activity'] = advance_activity(context.get('activity'), now(), 'assessment_end')
                if plans.sql:
                    row.context = context
                else:
                    row['context'] = context
