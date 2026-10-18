from datetime import date
import pytest

from knowpath_backend.learning.plans.rolling import allocate_ordered_tasks
from knowpath_backend.test.test_learning_plan_order import ordered_backend


def task(identifier, minutes, **context):
    return {'id': identifier, 'node_id': identifier, 'topic_ids': [identifier],
            'kind': 'learn', 'status': 'pending', 'estimated_minutes': minutes,
            'context': {'position': 0, **context}}


def test_summary_reports_actual_forecast_capacity_and_unallocated_work():
    result = allocate_ordered_tasks([task(str(i), 20, duration_forecast={'study_minutes': 15, 'answer_minutes': 5}) for i in range(21)],
        {'daily_minutes': 30, 'horizon_days': 7}, {}, date(2026, 10, 12))
    assert result['summary'] == {'selected_minutes': 420, 'capacity_minutes': 210,
        'allocated_minutes': 210, 'unallocated_minutes': 210, 'unallocated_count': 11}


def test_unallocated_summary_includes_deferred_and_forecast_blocked_work():
    parent = task('parent', 10); parent['status'] = 'deferred'
    result = allocate_ordered_tasks([parent, task('child', 10, prerequisites=['parent'], duration_forecast={'study_minutes': 15, 'answer_minutes': 5})],
        {'daily_minutes': 30, 'horizon_days': 7}, {}, date(2026, 10, 12))
    assert result['summary']['selected_minutes'] == 30
    assert result['summary']['unallocated_minutes'] == 30
    assert result['summary']['unallocated_count'] == 2


def test_plan_preview_shares_creation_calculation_and_has_no_business_writes(ordered_backend, monkeypatch):
    from copy import deepcopy
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    from knowpath_backend.test.test_learning_plans_sessions import seed
    state, space_id, _ = seed(ordered_backend)
    payload = {'schedule_mode': 'budget_rolling', 'daily_minutes': 5, 'horizon_days': 1}
    before = deepcopy(state.plan_sessions._space_plans(space_id))
    preview = state.preview_plan(space_id, payload)
    assert state.plan_sessions._space_plans(space_id) == before
    plan = state.create_plan(space_id, payload)
    assert preview['summary']['selected_minutes'] == sum(t['estimated_minutes'] for t in plan['tasks'])
    assert preview['summary']['allocated_minutes'] == 5
    assert preview['summary']['unallocated_minutes'] == preview['summary']['selected_minutes'] - 5
    assert [t['context']['duration_forecast'] for t in preview['tasks']] == [t['context']['duration_forecast'] for t in plan['tasks']]
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    from knowpath_backend.learning.plans import service
    instant = service.now()
    monkeypatch.setattr(service, 'now', lambda: instant)
    current = deepcopy(state.plan_sessions._space_plans(space_id))
    before_session = deepcopy(state.plan_sessions._space_sessions(space_id))
    state.preview_plan(space_id, {**payload, 'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': 1})
    assert state.plan_sessions._space_plans(space_id) == current
    assert state.plan_sessions._space_sessions(space_id) == before_session


def test_http_plan_preview_is_authenticated_strict_and_creates_no_business_records():
    from copy import deepcopy
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    from knowpath_backend.test.test_learning_plans_sessions import seed
    app = create_app(settings=LearningSettings(local_token='preview-test-token'))
    state, space_id, _ = seed(InMemoryMaterialRepository())
    app.state.learning_state = state
    before = deepcopy(state.plan_sessions.repository.materials.assessment_data)
    runs_before = deepcopy(state.run_service.repository._runs)
    payload = {'daily_minutes': 30, 'horizon_days': 7, 'schedule_mode': 'budget_rolling'}
    url = f'/api/v1/learning-spaces/{space_id}/plan-preview'
    with TestClient(app) as client:
        assert client.post(url, json=payload).status_code == 401
        headers = {'X-Local-Token': 'preview-test-token'}
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()['preview'] is True
        assert 'run_id' not in response.json()
        assert response.json()['summary']['capacity_minutes'] > 0
        assert client.post(url, json={**payload, 'unknown': True}, headers=headers).status_code == 422
    assert state.plan_sessions.repository.materials.assessment_data == before
    assert state.run_service.repository._runs == runs_before


def test_source_review_forecast_does_not_reserve_unavailable_automatic_questions():
    from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
    from knowpath_backend.test.test_learning_plans_sessions import seed
    state, space_id, _ = seed(InMemoryMaterialRepository())
    item = task('chart', 10, topics=[{'id': 'chart', 'is_assessable': False, 'automatic_questions': False}])
    state.plan_sessions.forecast_tasks([item], state.space_service.get(space_id))
    assert item['context']['duration_forecast']['answer_minutes'] == 0
    assert item['context']['duration_forecast']['answer_basis'] == 'source_review_no_automatic_questions'


def test_summary_deducts_consumed_active_reservations_and_ignores_completed_history():
    parent = task('parent', 20, allocations=[{'date': '2026-10-12', 'phase': 'study', 'minutes': 20}])
    done = task('done', 20, allocations=[{'date': '2026-10-12', 'phase': 'study', 'minutes': 20}]); done['status'] = 'completed'
    result = allocate_ordered_tasks([parent, done], {'daily_minutes': 30, 'horizon_days': 1}, {}, date(2026, 10, 12),
        protected_ids=['parent'], spent_minutes={'2026-10-12': 10}, consumed_by_task={'parent': {'study_seconds': 600}})
    assert result['summary']['allocated_minutes'] == 10
    assert result['summary']['selected_minutes'] == 10
    assert result['summary']['capacity_minutes'] == 20


def test_protected_reservations_outside_current_window_remain_visible_as_unallocated_work():
    item = task('active', 20, allocations=[{'date': '2026-10-11', 'phase': 'study', 'minutes': 10},
        {'date': '2026-10-15', 'phase': 'study', 'minutes': 10}])
    result = allocate_ordered_tasks([item], {'daily_minutes': 30, 'horizon_days': 1}, {}, date(2026, 10, 12), protected_ids=['active'])
    assert result['summary']['selected_minutes'] == 20
    assert result['summary']['allocated_minutes'] == 0
    assert result['summary']['unallocated_minutes'] == 20
    assert result['summary']['unallocated_count'] == 1


def test_protected_summary_deducts_activity_beyond_reserved_portion():
    item = task('active', 20, duration_forecast={'study_minutes': 10, 'answer_minutes': 10},
        allocations=[{'date': '2026-10-12', 'phase': 'study', 'minutes': 3}], remaining_unallocated_minutes=17)
    result = allocate_ordered_tasks([item], {'daily_minutes': 3, 'horizon_days': 1}, {}, date(2026, 10, 12),
        protected_ids=['active'], consumed_by_task={'active': {'study_seconds': 300}}, spent_minutes={'2026-10-12': 5})
    assert result['summary']['selected_minutes'] == 15
    assert result['summary']['unallocated_minutes'] == 15
    assert result['tasks'][0] == item


@pytest.mark.parametrize('finish', [False, True])
def test_local_preview_and_create_deduct_real_activity_once_across_replans(ordered_backend, monkeypatch, finish):
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    from knowpath_backend.learning.plans import service
    from knowpath_backend.test.test_learning_plans_sessions import seed
    clock = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(service, 'now', lambda: clock.isoformat())
    state, space_id, _ = seed(ordered_backend)
    settings = {'schedule_mode': 'budget_rolling', 'daily_minutes': 30, 'horizon_days': 7}
    plan = state.create_plan(space_id, settings)
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    for _ in range(10):
        clock += timedelta(seconds=30)
        state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})
    if finish:
        state.finish_session(session['id'])
    initial = plan['config']['schedule_summary']['selected_minutes']
    for _ in range(2):
        payload = {**settings, 'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': plan['version']}
        before = deepcopy(state.plan_sessions._space_plans(space_id)), deepcopy(state.plan_sessions._space_sessions(space_id))
        preview = state.preview_plan(space_id, payload)
        assert preview['summary']['selected_minutes'] == initial - 5
        assert (state.plan_sessions._space_plans(space_id), state.plan_sessions._space_sessions(space_id)) == before
        assert preview['tasks'][0]['context']['accumulated']['study_seconds'] == 300
        assert preview['tasks'][1]['context']['accumulated']['study_seconds'] == 0
        plan = state.create_plan(space_id, payload)
        assert plan['config']['schedule_summary']['selected_minutes'] == initial - 5
        assert state.get_plan(plan['id'])['config']['schedule_summary']['selected_minutes'] == initial - 5


def test_answer_activity_without_session_follows_stable_node_across_copied_tasks(ordered_backend, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from knowpath_backend.learning.plans import service
    from knowpath_backend.learning.assessments import service as assessments
    from knowpath_backend.test.test_learning_plans_sessions import seed
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    clock = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(service, 'now', lambda: clock.isoformat())
    monkeypatch.setattr(assessments, 'now', lambda: clock.isoformat())
    state, space_id, _ = seed(ordered_backend)
    state.assessment_service.generator = FixedQuestions()
    settings = {'schedule_mode': 'budget_rolling', 'daily_minutes': 30, 'horizon_days': 7}
    plan = state.create_plan(space_id, settings)
    first = plan['tasks'][0]
    created = state.create_assessment(space_id, {'kind': 'practice', 'question_count': 5,
        'question_types': ['single_choice'], 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0},
        'topic_ids': first['topic_ids'], 'plan_id': plan['id'], 'task_id': first['id']})
    assessment = state.get_assessment(created['id'])
    qid = assessment['questions'][0]['id']
    state.assessment_service.activity(assessment['id'], {'question_id': qid, 'event': 'shown'})
    for _ in range(4):
        clock += timedelta(seconds=30)
        state.assessment_service.activity(assessment['id'], {'question_id': qid, 'event': 'heartbeat'})
    for _ in range(2):
        payload = {**settings, 'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': plan['version']}
        preview = state.preview_plan(space_id, payload)
        assert preview['tasks'][0]['context']['accumulated']['answer_seconds'] == 120
        assert preview['tasks'][1]['context']['accumulated']['answer_seconds'] == 0
        assert preview['summary']['selected_minutes'] == 38
        plan = state.create_plan(space_id, payload)
        assert plan['config']['schedule_summary']['selected_minutes'] == 38


def test_deferred_remaining_work_is_unallocated_consistently_without_read_writes(ordered_backend, monkeypatch):
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    from knowpath_backend.learning.plans import service
    from knowpath_backend.test.test_learning_plans_sessions import seed
    clock = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(service, 'now', lambda: clock.isoformat())
    state, space_id, _ = seed(ordered_backend)
    settings = {'schedule_mode': 'budget_rolling', 'daily_minutes': 30, 'horizon_days': 7}
    plan = state.create_plan(space_id, settings)
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    for _ in range(10):
        clock += timedelta(seconds=30)
        state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})
    state.finish_session(session['id'])
    state.update_task(plan['id'], plan['tasks'][0]['id'], {'status': 'deferred', 'expected_plan_version': 1, 'defer_until': '2099-01-01T00:00:00+00:00'})
    before = deepcopy(state.plan_sessions._space_plans(space_id))
    current = state.get_plan(plan['id'])
    payload = {**settings, 'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'], 'expected_plan_version': current['version']}
    preview = state.preview_plan(space_id, payload)
    assert state.plan_sessions._space_plans(space_id) == before
    created = state.create_plan(space_id, payload)
    for summary in [current['config']['schedule_summary'], preview['summary'], created['config']['schedule_summary'], state.get_plan(created['id'])['config']['schedule_summary']]:
        assert summary['selected_minutes'] == 35
        assert summary['allocated_minutes'] == 20
        assert summary['unallocated_minutes'] == 15
        assert summary['unallocated_count'] == 1


def test_one_node_spans_days_without_new_identity():
    original = [task('first', 25), task('second', 10)]
    result = allocate_ordered_tasks(original, {'daily_minutes': 15, 'horizon_days': 3}, {}, date(2026, 10, 12))
    assert [t['id'] for t in result['tasks']] == ['first', 'second']
    assert [a['minutes'] for a in result['tasks'][0]['context']['allocations']] == [15, 10]
    assert result['tasks'][1]['context']['allocations'][0]['date'] == '2026-10-13'
    assert original[0]['context'].get('allocations') is None


def test_capacity_retains_all_nodes_and_does_not_defer():
    result = allocate_ordered_tasks([task('first', 25), task('second', 10)],
        {'daily_minutes': 15, 'horizon_days': 1}, {}, date(2026, 10, 12))
    assert len(result['tasks']) == 2
    assert result['tasks'][0]['context']['allocation_state'] == 'partial'
    assert result['tasks'][1]['context']['allocation_state'] == 'waiting_capacity'
    assert result['tasks'][1]['status'] == 'pending'


def test_deadline_and_weekly_budget_include_reserved_actual_time():
    result = allocate_ordered_tasks([task('first', 25)],
        {'daily_minutes': 15, 'horizon_days': 7},
        {'weekly_minutes': 20, 'target_date': '2026-10-13'}, date(2026, 10, 12),
        spent_minutes={'2026-10-12': 10})
    allocations = result['tasks'][0]['context']['allocations']
    assert sum(a['minutes'] for a in allocations) == 10
    assert all(a['date'] <= '2026-10-13' for a in allocations)
    assert result['remaining_minutes'] == 15


def test_explicit_deferred_prerequisite_blocks_dependent():
    parent = task('parent', 10); parent.update(status='deferred', defer_until='2026-11-01T00:00:00+00:00')
    result = allocate_ordered_tasks([parent, task('child', 10, prerequisites=['parent'])],
        {'daily_minutes': 30, 'horizon_days': 3}, {}, date(2026, 10, 12))
    assert result['tasks'][1]['context']['allocation_state'] == 'blocked'


def test_fixed_future_prerequisite_cannot_be_overtaken():
    parent = task('parent', 20, allocations=[{'date': '2026-10-13', 'phase': 'study', 'minutes': 20}])
    result = allocate_ordered_tasks([parent, task('child', 10, prerequisites=['parent'])],
        {'daily_minutes': 30, 'horizon_days': 3}, {}, date(2026, 10, 12), protected_ids=['parent'])
    assert result['tasks'][0] == parent
    assert result['tasks'][1]['context']['allocations'][0]['date'] == '2026-10-13'


def test_active_consumption_is_not_charged_twice_or_written_into_task():
    parent = task('parent', 20, allocations=[{'date': '2026-10-12', 'phase': 'study', 'minutes': 20}])
    result = allocate_ordered_tasks([parent, task('child', 10, prerequisites=['parent'])],
        {'daily_minutes': 30, 'horizon_days': 3}, {}, date(2026, 10, 12),
        protected_ids=['parent'], spent_minutes={'2026-10-12': 10},
        consumed_by_task={'parent': {'study_seconds': 600, 'answer_seconds': 0}})
    assert result['days'][0]['used_minutes'] == 30
    assert result['tasks'][1]['context']['allocations'][0]['date'] == '2026-10-12'
    assert result['tasks'][0] == parent


def test_consumption_applies_to_past_reservations_before_future_days():
    parent = task('parent', 20, allocations=[
        {'date': '2026-10-11', 'phase': 'study', 'minutes': 10},
        {'date': '2026-10-12', 'phase': 'study', 'minutes': 10}])
    result = allocate_ordered_tasks([parent, task('child', 10)],
        {'daily_minutes': 20, 'horizon_days': 2}, {}, date(2026, 10, 12), protected_ids=['parent'],
        spent_minutes={'2026-10-11': 10}, consumed_by_task={'parent': {'study_seconds': 600}})
    assert result['days'][0]['used_minutes'] == 20
    assert result['tasks'][1]['context']['allocations'][0]['date'] == '2026-10-12'


def test_fixed_partial_prerequisite_does_not_publish_executable_child():
    parent = task('parent', 40, allocation_state='partial', remaining_unallocated_minutes=20,
        allocations=[{'date': '2026-10-12', 'phase': 'study', 'minutes': 20}])
    result = allocate_ordered_tasks([parent, task('child', 10, prerequisites=['parent'])],
        {'daily_minutes': 30, 'horizon_days': 3}, {}, date(2026, 10, 12), protected_ids=['parent'])
    assert result['tasks'][1]['context']['allocation_state'] == 'blocked'
    assert result['tasks'][1]['context']['allocation_reason'] == 'prerequisite_capacity'
