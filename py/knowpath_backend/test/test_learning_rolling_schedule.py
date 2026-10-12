from datetime import date

from knowpath_backend.learning.plans.rolling import allocate_ordered_tasks


def task(identifier, minutes, **context):
    return {'id': identifier, 'node_id': identifier, 'topic_ids': [identifier],
            'kind': 'learn', 'status': 'pending', 'estimated_minutes': minutes,
            'context': {'position': 0, **context}}


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
