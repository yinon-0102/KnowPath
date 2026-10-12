"""Actionable current evidence recommendations, never causal policy rankings."""
from copy import deepcopy
from uuid import uuid4

from .replay import ReplayService
from ..assessments.review_policy import evidence_sufficiency, effective_evidence, enrich_observation_times, independent_evidence
from ..assessments.service import revision
from ..errors import DomainConflict
from ..plans.adjustments import now, digest


class PolicyRecommendationService:
    def __init__(self, plans, adjustments):
        self.plans, self.adjustments = plans, adjustments
        self.repository, self.spaces = plans.repository, plans.spaces

    def create(self, space_id, payload, key=None):
        def change():
            space = self.spaces.repository.get(space_id)
            current = self.plans._current_plan_id(space_id)
            plan = self.adjustments._raw(current) if current else None
            states = self.plans._planning_states(space_id)
            topics = self.plans._ordered_topics(space, states, plan['config'] if plan else {})
            epochs = self.plans.assessments._epochs(space_id)
            evidence = enrich_observation_times(self.repository, self.repository.records('evidence', space_id=space_id, lock=False))
            missing, weak, due, summaries = [], [], [], []
            for topic in topics:
                rows = effective_evidence([e for e in evidence if e['topic_id'] == topic['id']],
                    revision_id=revision(topic), epoch=epochs.get(topic['id'], 0), as_of=now())
                eligible = [e for e in rows if e.get('eligible') and not e.get('assisted') and not e.get('revoked_by_review_id') and e.get('score') is not None]
                eligible = independent_evidence(eligible)
                sufficiency = evidence_sufficiency(eligible)
                state = states.get(topic['id'], {})
                summaries.append({'topic_id': topic['id'], 'name': topic['name'], 'evidence_ids': [e['id'] for e in eligible],
                                  'sufficiency': sufficiency, 'status': state.get('status', 'unseen')})
                if eligible and any(e['score'] < .6 for e in eligible) and state.get('score_validity') != 'stale':
                    weak.append(topic['id'])
                elif sufficiency.get('status') != 'sufficient':
                    missing.append(topic['id'])
                if state.get('review_due'):
                    due.append(topic['id'])
            if weak:
                action = {'kind': 'diagnostic', 'assessment_kind': 'practice', 'topic_ids': weak[:5], 'adaptive': False}
                explanation = '观察到有效独立作答错误，建议先进行对应知识点的巩固练习。'
            elif due:
                action = {'kind': 'diagnostic', 'assessment_kind': 'retest', 'topic_ids': due[:5], 'adaptive': False}
                explanation = '已有独立证据对应的复习到期，建议完成独立复测。'
            elif missing:
                action = {'kind': 'diagnostic', 'assessment_kind': 'diagnostic', 'topic_ids': missing[:5], 'adaptive': True}
                explanation = '这些主题的掌握证据仍不足，未知不等于薄弱；建议补充独立诊断。'
            elif plan and any(t.get('context', {}).get('allocation_state') in {'waiting_capacity', 'partial'} for t in plan['tasks']):
                daily = max(1, min(480, plan['config'].get('daily_minutes', 30)))
                action = {'kind': 'budget', 'budgets_minutes_per_day': sorted({daily, daily + 15 if daily <= 465 else daily - 15}), 'horizon_days': 7}
                explanation = '当前预算存在待排节点，可比较不同预算后再确认调整。'
            else:
                action, explanation = {'kind': 'keep'}, '当前没有需要立即调整的有效证据，保留现有学习安排。'
            try:
                report = ReplayService(self.plans.assessments).replay(space_id, {'limit': 100, 'minimum_delay_hours': 24})
                history = {k: report[k] for k in ('policy_version', 'causal_effect_estimated', 'policies', 'prediction', 'observed_retests', 'snapshot_hash', 'limitations')}
            except DomainConflict as exc:
                history = {'status': 'unavailable', 'reason': exc.code, 'causal_effect_estimated': False}
            snapshot = {'plan_id': current, 'plan_version': plan['version'] if plan else None,
                        'scope_version': space['scope_version'], 'profile_version': space['profile_version'],
                        'state_version': space['state_version'], 'bindings': space['bindings']}
            record = {'id': str(uuid4()), 'space_id': space_id, 'plan_id': current, 'status': 'ready', 'run_id': None,
                'snapshot': snapshot, 'explanation': explanation, 'action': action, 'evidence': summaries,
                'missing_evidence': missing, 'history': history, 'policy_version': 'current-evidence-recommendation-v1', 'created_at': now()}
            self.repository.put_record('policy_recommendations', record)
            return record
        return self.plans.assessments._execute('policy.recommend', space_id, payload, key, change)

    def apply(self, identifier, payload, key=None):
        def change():
            record = self.repository.get_record('policy_recommendations', identifier, lock=False)
            space = self.spaces.repository.get(record['space_id'])
            snapshot = record['snapshot']
            plan = self.adjustments._raw(snapshot['plan_id']) if snapshot['plan_id'] else None
            if (record['status'] != 'ready' or space.get('status') == 'archived'
                    or any(snapshot[k] != space[k] for k in ('scope_version', 'profile_version', 'state_version', 'bindings'))
                    or self.plans._current_plan_id(space['id']) != snapshot['plan_id']
                    or plan and (plan['version'] != snapshot['plan_version'] or payload.get('expected_plan_version') != plan['version'])):
                raise DomainConflict('VERSION_CONFLICT', '建议依据或计划版本已变化，请重新生成')
            action = record['action']
            if payload.get('preview', True):
                return {'recommendation_id': identifier, 'can_apply': True, 'action': deepcopy(action),
                        'expected_plan_version': plan['version'] if plan else None, 'explanation': record['explanation']}
            if action['kind'] == 'diagnostic':
                result = self.plans.assessments.create(space['id'], {'kind': action['assessment_kind'], 'topic_ids': action['topic_ids'],
                    'question_count': 5, 'question_types': ['single_choice'], 'adaptive': action['adaptive']}, durable=True)
            elif action['kind'] == 'budget':
                result = self.adjustments.compare(space['id'], {k: action[k] for k in ('budgets_minutes_per_day', 'horizon_days')})
            else:
                result = {'status': 'unchanged', 'plan_id': snapshot['plan_id']}
            record.update(status='applied', applied_at=now(), result=deepcopy(result))
            self.repository.put_record('policy_recommendations', record)
            return {'recommendation_id': identifier, 'status': 'applied', 'action': action, 'result': result}
        return self.plans.assessments._execute('policy.apply', identifier, payload, key, change)
