import pytest
from knowpath_backend.test.test_learning_personalization import learning
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
from knowpath_backend.learning.plans.activity import new_activity, advance_activity


def linked(learning, kind='practice'):
    state, space, _ = learning
    state.assessment_service.generator = FixedQuestions()
    plan = state.create_plan(space, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    def create():
        result = state.create_assessment(space, {'kind': kind, 'question_count': 5,
            'question_types': ['single_choice'], 'difficulty_mix': {'easy': 1., 'medium': 0., 'hard': 0.},
            'learning_session_id': session['id']})
        return state.get_assessment(result['id'])
    return state, session, create


@pytest.mark.parametrize('kind', ['practice', 'retest'])
def test_end_after_submitted_question_resumes_reading_without_finishing_round(learning, monkeypatch, kind):
    import knowpath_backend.learning.assessments.service as service
    import knowpath_backend.learning.plans.service as plans
    clock = ['2026-10-17T00:00:00+00:00']
    monkeypatch.setattr(service, 'now', lambda: clock[0])
    monkeypatch.setattr(plans, 'now', lambda: clock[0])
    state, session, create = linked(learning, kind)
    assessment = create()
    qid = assessment['questions'][0]['id']
    clock[0] = '2026-10-17T00:00:10+00:00'
    state.assessment_service.activity(assessment['id'], {'question_id': qid, 'phase_id': 'phase-a', 'event': 'shown'})
    clock[0] = '2026-10-17T00:00:20+00:00'
    state.record_attempt(assessment['id'], {'answers': [{'question_id': qid, 'expected_answer_revision': 0, 'answer': 'A'}]})
    clock[0] = '2026-10-17T00:00:25+00:00'
    body = {'phase_id': 'phase-a', 'event': 'assessment_end'}
    ended = state.assessment_service.activity(assessment['id'], body, 'end-a')
    assert state.assessment_service.activity(assessment['id'], body, 'end-a') == ended
    clock[0] = '2026-10-17T00:00:55+00:00'
    result = state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})
    assert result['activity']['active_seconds'] == 40
    assert 'assessment' not in result['activity']['pause_reasons']
    assert state.assessment_service.repository.get_record('sessions', session['id'])['status'] == 'active'
    restored = state.get_assessment(assessment['id'])
    assert restored['status'] == 'in_progress'
    assert len(restored['answers']) == 1
    assert state.assessment_service.repository.get_record('assessments', assessment['id'])['snapshot']['answer_activity'][qid]['active_seconds'] == 10


def test_old_end_or_shown_cannot_clear_or_replace_new_phase_owner(learning):
    state, session, create = linked(learning)
    a, b = create(), create()
    for assessment, phase in [(a, 'a'), (b, 'b')]:
        state.assessment_service.activity(assessment['id'], {'question_id': assessment['questions'][0]['id'], 'phase_id': phase, 'event': 'shown'})
    state.assessment_service.activity(a['id'], {'phase_id': 'a', 'event': 'assessment_end'})
    state.assessment_service.activity(a['id'], {'question_id': a['questions'][0]['id'], 'phase_id': 'a', 'event': 'shown'})
    result = state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})
    assert 'assessment' in result['activity']['pause_reasons']
    state.assessment_service.activity(b['id'], {'phase_id': 'b', 'event': 'assessment_end'})
    assert 'assessment' not in state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})['activity']['pause_reasons']


def test_phase_end_does_not_clear_independent_disconnection_or_other_pauses():
    state = new_activity(1000)
    for event in ['assessment_start', 'pause', 'hidden', 'disconnect']:
        state = advance_activity(state, 1000, event)
    assert advance_activity(state, 1010, 'assessment_end')['pause_reasons'] == ['disconnected', 'hidden', 'manual_pause']


def test_public_end_schema_is_independent_of_current_question():
    from knowpath_backend.learning.api.routers.personalization import AnswerActivity
    payload = AnswerActivity.model_validate({'event': 'assessment_end', 'phase_id': 'a'})
    assert payload.event == 'assessment_end'
    with pytest.raises(ValueError):
        AnswerActivity.model_validate({'event': 'shown', 'phase_id': 'a'})


def test_real_http_end_contract_accepts_submitted_question_and_replays(learning):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    state, session, create = linked(learning)
    assessment = create()
    app = create_app()
    app.state.learning_state = state
    client = TestClient(app)
    url = f"/api/v1/assessments/{assessment['id']}/activity"
    qid = assessment['questions'][0]['id']
    shown = client.post(url, json={'event': 'shown', 'question_id': qid, 'phase_id': 'http-a'}, headers={'Idempotency-Key': 'http-shown'})
    assert shown.status_code == 200, shown.text
    state.record_attempt(assessment['id'], {'answers': [{'question_id': qid, 'expected_answer_revision': 0, 'answer': 'A'}]})
    body = {'event': 'assessment_end', 'phase_id': 'http-a'}
    first = client.post(url, json=body, headers={'Idempotency-Key': 'http-end'})
    assert first.status_code == 200, first.text
    assert client.post(url, json=body, headers={'Idempotency-Key': 'http-end'}).json() == first.json()
    assert client.post(url, json={'event': 'shown', 'phase_id': 'invalid'}, headers={'Idempotency-Key': 'http-invalid'}).status_code == 422
    assert client.post(url, json={**body, 'learning_session_id': session['id']}, headers={'Idempotency-Key': 'http-foreign'}).status_code == 422
    schema = app.openapi()['components']['schemas']['AnswerActivity']
    assert schema['properties']['phase_id']['anyOf'][0] == {'type': 'string', 'minLength': 1, 'maxLength': 128}
    assert 'assessment_end' in schema['properties']['event']['enum']
    assert schema.get('required', []) == []


def test_legacy_session_without_phase_owner_can_end_its_original_question(learning):
    state, session, create = linked(learning)
    assessment = create()
    qid = assessment['questions'][0]['id']
    state.assessment_service.activity(assessment['id'], {'event': 'shown', 'question_id': qid})
    with state.assessment_service.repository.transaction():
        row = state.plan_sessions._find_session_sql(session['id'], lock=True) if state.plan_sessions.sql else state.plan_sessions._load_session_memory(session['id'])
        context = dict(row.context if state.plan_sessions.sql else row['context'])
        context.pop('assessment_phase')
        if state.plan_sessions.sql:
            row.context = context
        else:
            row['context'] = context
    state.assessment_service.activity(assessment['id'], {'event': 'assessment_end', 'question_id': qid})
    assert 'assessment' not in state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})['activity']['pause_reasons']


def test_end_before_delayed_shown_tombstones_the_phase(learning):
    state, session, create = linked(learning)
    assessment = create()
    state.assessment_service.activity(assessment['id'], {'event': 'assessment_end', 'phase_id': 'late'})
    state.assessment_service.activity(assessment['id'], {'event': 'shown', 'phase_id': 'late', 'question_id': assessment['questions'][0]['id']})
    assert 'assessment' not in state.plan_sessions.activity(session['id'], {'event': 'heartbeat'})['activity']['pause_reasons']


def test_phase_end_preserves_session_pause_reasons_and_checks_sealed_ownership(learning):
    from knowpath_backend.learning.errors import DomainConflict
    state, session, create = linked(learning)
    assessment = create()
    body = {'event': 'shown', 'phase_id': 'owner', 'question_id': assessment['questions'][0]['id']}
    state.assessment_service.activity(assessment['id'], body)
    for event in ['pause', 'hidden', 'disconnect']:
        state.plan_sessions.activity(session['id'], {'event': event})
    state.assessment_service.activity(assessment['id'], {'event': 'assessment_end', 'phase_id': 'owner'})
    with state.assessment_service.repository.transaction():
        row = state.plan_sessions._find_session_sql(session['id']) if state.plan_sessions.sql else state.plan_sessions._load_session_memory(session['id'])
        context = row.context if state.plan_sessions.sql else row['context']
        assert context['activity']['pause_reasons'] == ['disconnected', 'hidden', 'manual_pause']
    with state.assessment_service.repository.transaction():
        record = state.assessment_service.repository.get_record('assessments', assessment['id'])
        record['task_id'] = 'foreign-task'
        state.assessment_service.repository.put_record('assessments', record)
    with pytest.raises(DomainConflict, match='归属'):
        state.assessment_service.activity(assessment['id'], {**body, 'phase_id': 'foreign'})
