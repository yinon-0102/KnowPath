"""Completion recovery identifies the original write and never repeats it."""
import copy

import pytest

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.test.test_learning_workbench import workbench, assessment


def complete(state, plan, task, key='completion'):
    body = {'expected_plan_version': plan['version'], 'status': 'completed', 'source': 'manual'}
    return body, state.update_task(plan['id'], task['id'], body, idempotency_key=key)


def test_completion_has_receipt_and_read_only_recovery(workbench, monkeypatch):
    factory, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    body, done = complete(state, plan, task)
    assert done['space_id'] == space_id
    assert done['completion_receipt'] == {
        'space_id': space_id, 'plan_id': plan['id'], 'task_id': task['id'],
        'plan_version': 2, 'event_id': done['context']['completion']['event_id'], 'source': 'manual'}
    assert done['continuation_context']['space_id'] == space_id
    assert done['continuation_context']['current_plan']['version'] == 2
    assert done['continuation_context']['active_session'] is None
    summary = done['continuation_context']['round_summary']
    assert summary['plan_id'] == plan['id'] and summary['plan_version'] == 2
    assert summary['tasks'] == done['task_progress']
    assert summary['processed'] == (summary['tasks']['remaining'] == 0)
    assert summary['details_pending'] is True
    assert 'mastery' not in summary and 'assessments' not in summary
    state = factory()
    before = copy.deepcopy(state.get_plan(plan['id']))
    for name in ('get_plan', 'get_workbench', 'get_progress', '_planning_states'):
        monkeypatch.setattr(state.plan_sessions, name, lambda *a, **k: pytest.fail('heavy read'))
    recovered = state.task_completion_status(plan['id'], task['id'], body, idempotency_key='completion')
    assert recovered['status'] == 'confirmed'
    assert recovered['result']['completion_receipt'] == done['completion_receipt']
    assert recovered['result']['next_action']['kind'] in {'task', 'summary'}
    assert factory().get_plan(plan['id']) == before
    assert state.task_completion_status(plan['id'], task['id'], body, idempotency_key='different')['status'] == 'unknown'
    with pytest.raises(DomainConflict) as exc:
        state.task_completion_status(plan['id'], task['id'], {**body, 'note': 'different'}, idempotency_key='completion')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('change', ['undo', 'recomplete', 'replan'])
def test_original_receipt_is_superseded_after_undo_or_new_plan(workbench, change):
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    body, done = complete(state, plan, task)
    if change == 'replan':
        state.create_plan(space_id, {})
    else:
        state.undo_task_completion(plan['id'], task['id'], {'expected_plan_version': 2,
            'completion_event_id': done['context']['completion']['event_id'], 'reason': 'mistake'}, idempotency_key='undo')
        if change == 'recomplete':
            state.update_task(plan['id'], task['id'], {**body, 'expected_plan_version': 3}, idempotency_key='new')
    assert state.task_completion_status(plan['id'], task['id'], body, idempotency_key='completion')['status'] == 'superseded'


def test_current_continuation_prefers_session_and_assessment(workbench):
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    session = state.start_session(plan['id'], task['id'])
    body, done = complete(state, plan, task)
    assert done['next_action']['kind'] == 'session'
    assert done['next_action']['session_id'] == session['id']
    assert done['continuation_context']['active_session']['id'] == session['id']
    assert done['continuation_context']['active_session']['space_id'] == space_id
    state.finish_session(session['id'])
    first, second = assessment(state, space_id), assessment(state, space_id)
    result = state.task_completion_status(plan['id'], task['id'], body, idempotency_key='completion')['result']
    assert result['next_action']['kind'] == 'assessment'
    assert 'assessment_id' not in result['next_action']
    assert first['id'] != second['id']


def test_navigation_read_failure_keeps_successful_receipt(workbench, monkeypatch):
    factory, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    monkeypatch.setattr(state.plan_sessions, '_completion_continuation', lambda *a: (_ for _ in ()).throw(RuntimeError('read unavailable')))
    body, done = complete(state, plan, plan['tasks'][0])
    assert done['completion_receipt']['plan_version'] == 2
    assert done['navigation_pending'] is True and done['next_action'] is None
    assert factory().task_completion_status(plan['id'], plan['tasks'][0]['id'], body, idempotency_key='completion')['status'] == 'confirmed'


def test_scope_change_continuation_requires_replan(workbench):
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    body, _ = complete(state, plan, plan['tasks'][0])
    state.set_scope(space_id, {'topic_ids': state.get_space(space_id)['topic_ids'], 'expected_version': 2})
    result = state.task_completion_status(plan['id'], plan['tasks'][0]['id'], body, idempotency_key='completion')
    assert result['status'] == 'confirmed'
    assert result['result']['next_action']['kind'] == 'replan'
    assert result['result']['continuation_context']['current_plan']['status'] == 'needs_replan'


def test_query_does_not_claim_current_confirmation_when_current_read_fails(workbench, monkeypatch):
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    body, _ = complete(state, plan, plan['tasks'][0])
    monkeypatch.setattr(state.plan_sessions, '_completion_continuation', lambda *a: (_ for _ in ()).throw(RuntimeError('read unavailable')))
    with pytest.raises(RuntimeError, match='read unavailable'):
        state.task_completion_status(plan['id'], plan['tasks'][0]['id'], body, idempotency_key='completion')


def test_http_completion_status_uses_original_body_and_key(workbench):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    _, state, space_id, _, _ = workbench
    app = create_app()
    app.state.learning_state = state
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    body = {'expected_plan_version': 1, 'status': 'completed'}
    path = f"/api/v1/plans/{plan['id']}/tasks/{task['id']}"
    with TestClient(app) as client:
        assert client.post(path + '/completion-status', json=body).status_code == 400
        assert client.post(path + '/completion-status', json=body,
            headers={'Idempotency-Key': 'http'}).json() == {'status': 'unknown'}
        done = client.patch(path, json=body, headers={'Idempotency-Key': 'http'})
        assert done.status_code == 200
        recovered = client.post(path + '/completion-status', json=body, headers={'Idempotency-Key': 'http'})
        assert recovered.status_code == 200 and recovered.json()['status'] == 'confirmed'
        assert recovered.json()['result']['completion_receipt'] == done.json()['completion_receipt']
        assert client.post(path + '/completion-status', json={**body, 'note': 'new'},
            headers={'Idempotency-Key': 'http'}).status_code == 409


def test_recovery_sql_has_no_write_lock_or_answer_history_reads(workbench):
    from sqlalchemy import event
    _, state, space_id, _, _ = workbench
    if not state.plan_sessions.sql:
        pytest.skip('SQL contract')
    plan = state.create_plan(space_id, {})
    body, _ = complete(state, plan, plan['tasks'][0])
    queries = []
    def record(_, __, statement, *args):
        queries.append(statement.upper())
    engine = state.plan_sessions.uow.engine
    event.listen(engine, 'before_cursor_execute', record)
    try:
        state.task_completion_status(plan['id'], plan['tasks'][0]['id'], body, idempotency_key='completion')
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert queries
    assert not any('FOR UPDATE' in query or query.startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'BEGIN IMMEDIATE')) for query in queries)
    assert not any(name in query for query in queries for name in ('FROM ATTEMPTS', 'FROM EVIDENCE', 'FROM SESSION_EVENTS', 'FROM LEARNER_STATES'))


def test_assessment_receipt_preserves_exact_assessment_and_original_version(workbench):
    from knowpath_backend.test.test_learning_plans_sessions import linked_practice
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    linked = linked_practice(state, space_id, plan, task, complete=True)
    body = {'expected_plan_version': 1, 'status': 'completed', 'source': 'assessment', 'assessment_id': linked['id']}
    done = state.update_task(plan['id'], task['id'], body, idempotency_key='assessment-completion')
    assert done['completion_receipt']['assessment_id'] == linked['id']
    assert done['completion_receipt']['source'] == 'assessment'
    assert state.task_completion_status(plan['id'], task['id'], body,
        idempotency_key='assessment-completion')['result']['completion_receipt'] == done['completion_receipt']


def test_new_plan_version_does_not_rewrite_original_receipt(workbench):
    _, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    body, original = complete(state, plan, task)
    state.update_task(plan['id'], task['id'], {**body, 'expected_plan_version': 2, 'note': 'later note'}, idempotency_key='later')
    recovered = state.task_completion_status(plan['id'], task['id'], body, idempotency_key='completion')
    assert recovered['status'] == 'confirmed'
    assert recovered['result']['completion_receipt'] == original['completion_receipt']
    assert recovered['result']['plan_version'] == 2
    assert recovered['result']['current_plan_version'] == 3
    assert recovered['result']['continuation_context']['current_plan']['version'] == 3
    with pytest.raises(DomainConflict) as exc:
        state.task_completion_status(plan['id'], 'different-task', body, idempotency_key='completion')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
