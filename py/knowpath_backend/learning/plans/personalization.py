"""Activity persistence and phase forecasts without changing learning identity."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from math import ceil

from .activity import advance_activity, activity_snapshot, activity_daily_seconds
from .duration import predict_duration
from ..errors import DomainConflict, DomainNotFound


class PlanPersonalization:
    def activity(self, session_id, payload, key=None):
        from .service import now
        event = payload.get('event', 'heartbeat')
        allowed = {'heartbeat', 'pause', 'resume', 'hidden', 'visible', 'disconnect', 'reconnect', 'waiting', 'ready'}
        if event not in allowed:
            raise DomainConflict('INVALID_REQUEST', '学习计时事件无效')

        def change():
            row = self._find_session_sql(session_id) if self.sql else self._load_session_memory(session_id)
            if row is None:
                raise DomainNotFound('session', session_id)
            space_id = row.space_id if self.sql else row['space_id']
            self.spaces.repository.get(space_id)
            row = self._find_session_sql(session_id, lock=True) if self.sql else self._load_session_memory(session_id)
            status = row.status if self.sql else row['status']
            if status != 'active':
                raise DomainConflict('SESSION_FINISHED', '会话已结束')
            context = deepcopy(row.context if self.sql else row.get('context', {}))
            context['activity'] = advance_activity(context.get('activity'), now(), event)
            context['paused'] = bool(context['activity']['pause_reasons'])
            if self.sql:
                row.context = context
            else:
                row['context'] = context
                self.repository.materials.assessment_data['sessions'][session_id] = deepcopy(row)
            return {'session_id': session_id, 'activity': deepcopy(context['activity']), 'paused': context['paused']}
        return self.assessments._execute('session.activity', session_id, payload, key, change)

    def duration_samples(self, space_id):
        space = self.spaces.repository.get(space_id)
        bound = {(b['material_id'], b['material_version_id']) for b in space['bindings']}
        scope = {t['id'] for t in self._ordered_topics(space, {})}
        plans = self._space_plans(space_id)
        tasks = {t['id']: t for p in plans for t in p['tasks']}
        grouped = defaultdict(list)
        for session in self._space_sessions(space_id):
            task = tasks.get(session['task_id'])
            context = session.get('context', {})
            activity = context.get('activity', {})
            refs = context.get('source_refs', [])
            if (not task or task['status'] != 'completed' or session['status'] != 'finished'
                    or activity.get('reliable') is not True or not refs
                    or activity.get('quality_flags')
                    or not set(context.get('topic_ids', task['topic_ids'])).issubset(scope)
                    or any((r['material_id'], r['material_version_id']) not in bound for r in refs)):
                continue
            grouped[context.get('node_id', task['node_id'])].append(session)
        samples = []
        for node, sessions in grouped.items():
            context = sessions[0]['context']
            seconds = sum(s['context']['activity']['active_seconds'] for s in sessions)
            if not 30 <= seconds <= 14400:
                continue
            baseline = context.get('duration_baseline', {}).get('study_minutes')
            if baseline is None:
                continue
            samples.append({'id': node, 'space_id': space_id, 'material_id': context['source_refs'][0]['material_id'],
                'kind': context['kind'], 'phase': 'study', 'active_seconds': seconds, 'baseline_minutes': baseline,
                'reliable': True, 'completed_at': max(s['finished_at'] for s in sessions)})
        from ..assessments.review_policy import effective_evidence
        evidence = self.repository.records('evidence', space_id=space_id, lock=False)
        assessments = self.repository.records('assessments', space_id=space_id, lock=False)
        epochs = self.assessments._epochs(space_id)
        from ..assessments.service import revision
        revisions = {t['id']: revision(t) for t in self.spaces.bound_topics(space)}
        evidence = [item for row in evidence for item in effective_evidence([row],
            revision_id=revisions.get(row['topic_id']), epoch=epochs.get(row['topic_id'], 0),
            as_of=datetime.now(timezone.utc).isoformat())]
        seen = set()
        for assessment in assessments:
            if assessment['status'] != 'completed':
                continue
            for question in assessment['questions']:
                if question['topic_id'] not in scope:
                    continue
                row = next((e for e in evidence if e['assessment_id'] == assessment['id'] and e['question_id'] == question['id']
                            and e.get('eligible') and not e.get('assisted') and not e.get('revoked_by_review_id')
                            and e.get('epoch', 0) == epochs.get(e['topic_id'], 0)
                            and e['topic_revision_id'] == revisions.get(e['topic_id'])), None)
                if not row or row['family_id'] in seen:
                    continue
                timing = assessment.get('snapshot', {}).get('answer_activity', {}).get(question['id'], {})
                if timing.get('reliable') is not True or timing.get('status') != 'finished' or timing.get('quality_flags'):
                    continue
                seconds = timing.get('active_seconds', 0)
                if not 1 <= seconds <= 3600:
                    continue
                associated_task = tasks.get(assessment.get('task_id'), {})
                refs = question.get('source_refs', [])
                if not refs or any((r.get('material_id'), r.get('material_version_id')) not in bound for r in refs):
                    continue
                source = refs[0]
                seen.add(row['family_id'])
                samples.append({'id': row['id'], 'space_id': space_id, 'material_id': source.get('material_id'),
                    'kind': associated_task.get('kind', 'learn'), 'phase': 'answer', 'active_seconds': seconds,
                    'baseline_minutes': 5 if question['type'] == 'short_answer' else 2, 'reliable': True,
                    'completed_at': row['created_at']})
        return samples

    def forecast_tasks(self, tasks, space, states=None, *, protected_ids=()):
        states = states if states is not None else self._planning_states(space['id'])
        prefs = space.get('profile', {}).get('preferences', {}).get('value') or {}
        samples = self.duration_samples(space['id']) if prefs.get('time_personalization', True) else []
        protected = set(protected_ids)
        for task in tasks:
            if task['id'] in protected or task['status'] in {'completed', 'skipped', 'deferred'} or task.get('context', {}).get('historical'):
                continue
            context = task.setdefault('context', {})
            refs = context.get('source_refs', [])
            material = refs[0].get('material_id') if refs else None
            prerequisites = []
            for parent in context.get('prerequisites', []) if prefs.get('time_personalization', True) else []:
                state = states.get(parent, {})
                effective = (state.get('score_validity') == 'current'
                    and state.get('independent_evidence_count', 0) > 0
                    and bool(state.get('error_tags')))
                prerequisites.append({'relevant': True, 'required': True, 'effective': effective,
                    'effective_difficulty': max(0, 1 - (state.get('mastery_score') or 0)) if effective else 0})
            forecast = predict_duration(task['kind'], space_id=space['id'], material_id=material, samples=samples, prerequisites=prerequisites)
            context['duration_forecast'] = forecast
            task['estimated_minutes'] = ceil(forecast['total_minutes'])
        return tasks

    def protected_task_ids(self, space_id):
        plans = self._space_plans(space_id)
        tasks = [t for p in plans for t in p['tasks']]
        by_id = {t['id']: t for t in tasks}
        started_nodes = {by_id[s['task_id']]['node_id'] for s in self._space_sessions(space_id) if s['task_id'] in by_id}
        for assessment in self.repository.records('assessments', space_id=space_id, lock=False):
            snap = assessment.get('snapshot', {})
            association = snap.get('plan_context') or snap.get('learning_context') or snap
            if association.get('node_id'):
                started_nodes.add(association['node_id'])
            task_id = assessment.get('task_id') or association.get('task_id')
            if task_id in by_id:
                started_nodes.add(by_id[task_id]['node_id'])
        return {t['id'] for t in tasks if t['node_id'] in started_nodes or t['status'] in {'completed', 'skipped', 'deferred'} or t.get('context', {}).get('historical')}

    def spent_minutes(self, space_id, config):
        from .service import now
        spent = defaultdict(float)
        for session in self._space_sessions(space_id):
            activity = activity_snapshot(session.get('context', {}).get('activity'), now())
            if activity.get('reliable'):
                for day, seconds in activity_daily_seconds(activity, config.get('calendar_timezone', 'Asia/Shanghai')).items():
                    spent[day] += seconds / 60
        for assessment in self.repository.records('assessments', space_id=space_id, lock=False):
            for timing in assessment.get('snapshot', {}).get('answer_activity', {}).values():
                timing = activity_snapshot(timing, now())
                if timing.get('reliable'):
                    for day, seconds in activity_daily_seconds(timing, config.get('calendar_timezone', 'Asia/Shanghai')).items():
                        spent[day] += seconds / 60
        return dict(spent)

    def accumulate_tasks(self, tasks, space_id):
        """Actual time is counted once per session/question, not per plan copy."""
        totals = self.task_elapsed(tasks, space_id)
        active = self._order_active_task_ids(space_id)
        for task in tasks:
            if task['id'] in active or task['status'] in {'completed', 'skipped', 'deferred'} or task.get('context', {}).get('historical'):
                continue
            task['context']['accumulated'] = totals[task['id']]
        return tasks

    def task_elapsed(self, tasks, space_id):
        from .service import now
        sessions = self.plans_sessions_for_accumulation(space_id)
        assessments = self.repository.records('assessments', space_id=space_id, lock=False)
        totals = {}
        for task in tasks:
            matching = [s for s in sessions if s.get('context', {}).get('node_id') == task['node_id']]
            study = sum(s.get('activity', {}).get('active_seconds', 0) for s in matching)
            answer = 0
            for assessment in assessments:
                if assessment.get('task_id') == task['id'] or assessment.get('learning_session_id') in {s['id'] for s in matching}:
                    answer += sum(activity_snapshot(a, now()).get('active_seconds', 0) for a in assessment.get('snapshot', {}).get('answer_activity', {}).values())
            totals[task['id']] = {'study_seconds': study, 'answer_seconds': answer}
        return totals

    def plans_sessions_for_accumulation(self, space_id):
        return self._space_sessions(space_id)
