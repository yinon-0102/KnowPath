import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_plans_sessions import seed
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions


@pytest.fixture(params=['memory', 'sql'])
def learning(request, tmp_path):
    engine = create_engine(f'sqlite+pysqlite:///{tmp_path / "personal.db"}') if request.param == 'sql' else None
    if engine:
        init_db(engine)
    state, space, topics = seed(SqlAlchemyMaterialRepository(engine) if engine else InMemoryMaterialRepository())
    yield state, space, topics
    if engine:
        engine.dispose()


def test_new_plan_uses_split_phase_rolling_forecasts(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {'daily_minutes': 15})
    assert plan['config']['schedule_mode'] == 'budget_rolling'
    assert plan['tasks'][0]['context']['duration_forecast']['answer_minutes'] == 10
    assert sum(a['minutes'] for a in plan['tasks'][0]['context']['allocations']) == plan['tasks'][0]['estimated_minutes']


def test_comparison_apply_is_atomic_and_keeps_node_identity(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {'daily_minutes': 15})
    comparison = state.plan_adjustments.compare(space, {'budgets_minutes_per_day': [15, 30], 'horizon_days': 7}, 'compare')
    payload = {'comparison_id': comparison['comparison_id'], 'scenario_id': comparison['scenarios'][1]['scenario_id'],
               'expected_plan_version': plan['version'], 'expected_snapshot_hash': comparison['snapshot']['hash']}
    applied = state.plan_adjustments.apply_schedule(plan['id'], payload, 'apply')
    assert applied['id'] == plan['id']
    assert [t['id'] for t in applied['tasks']] == [t['id'] for t in plan['tasks']]
    assert applied['config']['daily_minutes'] == 30
    assert state.plan_adjustments.apply_schedule(plan['id'], payload, 'apply') == applied
    with pytest.raises(DomainConflict, match='版本|快照'):
        state.plan_adjustments.apply_schedule(plan['id'], payload, 'stale')


def test_automatic_refresh_protects_started_node_and_is_idempotent(learning):
    state, space, topics = learning
    plan = state.create_plan(space, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    job = state.plan_adjustments.enqueue(space, 'test')
    result = state.plan_adjustments.run_once(job['event_id'])
    restored = state.get_plan(plan['id'])
    assert result
    assert restored['tasks'][0] == plan['tasks'][0]
    version = restored['version']
    assert not state.plan_adjustments.run_once(job['event_id'])
    assert state.get_plan(plan['id'])['version'] == version
    assert state.get_space(space)['active_session_id'] == session['id']


def test_server_activity_survives_restart_and_manual_pause(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    state.plan_sessions.activity(session['id'], {'event': 'pause'}, 'pause')
    state.plan_sessions.activity(session['id'], {'event': 'hidden'}, 'hide')
    second = LearningState(state.material_repository)
    restored = second.plan_sessions.activity(session['id'], {'event': 'visible'}, 'show')
    assert 'manual_pause' in restored['activity']['pause_reasons']
    finished = second.finish_session(session['id'])
    assert finished['activity']['status'] == 'finished'


def test_assessment_settlement_survives_rule_failure_and_deduplicates(learning):
    state, space, _ = learning
    state.assessment_service.generator = FixedQuestions()
    plan = state.create_plan(space, {})
    task = plan['tasks'][0]
    created = state.create_assessment(space, {'kind': 'practice', 'question_count': 5,
        'question_types': ['single_choice'], 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0},
        'plan_id': plan['id'], 'task_id': task['id']}, idempotency_key='linked')
    assessment = state.get_assessment(created['id'])
    assert assessment['status'] == 'ready'
    state.record_attempt(assessment['id'], {'answers': [{'question_id': q['id'],
        'expected_answer_revision': 0, 'answer': 'B'} for q in assessment['questions']], 'finalize': True}, idempotency_key='submit')
    result = state.assessment_service.result(assessment['id'])
    adjustment = result['adjustment']
    assert adjustment['status'] == 'queued'
    state.finalize_assessment(assessment['id'], {'allow_unanswered': False})
    assert len(state.plan_sessions.repository.records('plan_adjustments', plan_id=plan['id'])) == 1
    # A source/scope change invalidates publication, never the settled score.
    current = state.get_space(space)
    state.set_scope(space, {'topic_ids': task['topic_ids'], 'expected_version': current['space_version']})
    state.plan_adjustments.run_once()
    assert state.plan_adjustments.latest(plan['id'])['status'] == 'blocked'
    assert state.assessment_service.result(assessment['id'])['question_results'][0]['score'] == 0


def test_plan_refresh_does_not_mutate_identity_or_repeat_on_plain_reads(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {})
    job = state.plan_adjustments.enqueue(space, 'test')
    state.plan_adjustments.run_once(job['event_id'])
    after = state.get_plan(plan['id'])
    identities = [(t['id'], t['node_id'], t['sequence']) for t in after['tasks']]
    for _ in range(3):
        assert state.get_plan(plan['id'])['version'] == after['version']
    state.plan_adjustments.tick()
    assert not state.plan_adjustments.run_once()
    assert identities == [(t['id'], t['node_id'], t['sequence']) for t in plan['tasks']]


def test_revert_preview_is_read_only_and_matches_application(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {'daily_minutes': 15})
    comparison = state.plan_adjustments.compare(space, {'budgets_minutes_per_day': [15, 30], 'horizon_days': 7})
    applied = state.plan_adjustments.apply_schedule(plan['id'], {'comparison_id': comparison['comparison_id'],
        'scenario_id': comparison['scenarios'][1]['scenario_id'], 'expected_plan_version': 1,
        'expected_snapshot_hash': comparison['snapshot']['hash']})
    record = state.plan_adjustments.latest(plan['id'])
    preview = state.plan_adjustments.revert(record['id'], {'expected_plan_version': applied['version']})
    assert state.get_plan(plan['id'])['version'] == applied['version']
    reverted = state.plan_adjustments.revert(record['id'], {'expected_plan_version': applied['version'], 'apply': True})
    assert reverted['config']['daily_minutes'] == 15
    expected = {c['task_id']: c['after'] for c in preview['changes']}
    assert all(t == expected.get(t['id'], t) for t in reverted['tasks'])


def test_queued_refresh_respects_switch_changed_before_publication(learning):
    state, space, _ = learning
    plan = state.create_plan(space, {})
    job = state.plan_adjustments.enqueue(space, 'test')
    state.update_profile(space, {'expected_version': 1, 'preferences': {'automatic_adjustment': False}})
    state.plan_adjustments.run_once(job['event_id'])
    assert state.get_plan(plan['id'])['version'] == plan['version']
    assert state.plan_adjustments.latest(plan['id'])['error']['code'] == 'AUTOMATIC_ADJUSTMENT_DISABLED'


def test_budget_counts_local_midnight_and_no_double_answer_time(learning, monkeypatch):
    import knowpath_backend.learning.plans.service as service
    state, space, _ = learning
    clock = ['2026-10-12T15:59:30+00:00']
    monkeypatch.setattr(service, 'now', lambda: clock[0])
    plan = state.create_plan(space, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    clock[0] = '2026-10-12T16:00:30+00:00'
    state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})
    spent = state.plan_sessions.spent_minutes(space, {'calendar_timezone': 'Asia/Shanghai'})
    assert spent['2026-10-12'] == pytest.approx(.5)
    assert spent['2026-10-13'] == pytest.approx(.5)
    clock[0] = '2026-10-12T16:05:00+00:00'
    spent = state.plan_sessions.spent_minutes(space, {'calendar_timezone': 'Asia/Shanghai'})
    assert sum(spent.values()) == pytest.approx(2.5)  # 60s + bounded 90s


def test_personalization_http_preview_and_strict_activity(monkeypatch):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    app = create_app()
    state = app.state.learning_state
    material = state.material_service.create(filename='notes.md', content=b'# Loop\nLoops repeat work.', idempotency_key='api-source')
    space = state.create_space({'name': 'API', 'material_ids': [material.material.id]})['id']
    plan = state.create_plan(space, {})
    client = TestClient(app)
    recommendation = client.post(f'/api/v1/learning-spaces/{space}/policy-recommendations', json={}, headers={'Idempotency-Key': 'recommend'})
    assert recommendation.status_code == 200, recommendation.text
    preview = client.post(f"/api/v1/policy-recommendations/{recommendation.json()['id']}/apply",
        json={'expected_plan_version': 1, 'preview': True}, headers={'Idempotency-Key': 'preview'})
    assert preview.status_code == 200, preview.text
    assert state.get_plan(plan['id'])['version'] == 1
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    url = f"/api/v1/sessions/{session['id']}/activity"
    assert client.post(url, json={'event': 'heartbeat', 'elapsed_seconds': 999}, headers={'Idempotency-Key': 'bad-event'}).status_code == 422
    assert client.post(url, json={'event': 'hidden'}, headers={'Idempotency-Key': 'hide-event'}).status_code == 200
    assert client.get(f"/api/v1/plans/{plan['id']}/adjustments").status_code == 200
