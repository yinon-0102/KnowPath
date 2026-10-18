from knowpath_backend.test.test_learning_workbench import workbench
import pytest


@pytest.mark.parametrize('scope', ['question', 'topic', 'all'])
def test_session_hint_records_receipt_time_and_deduplicates_replay(workbench, scope):
    factory, state, sid, topic, clock = workbench
    state.assessment_service.clock = lambda: clock[0]
    plan = state.create_plan(sid, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    questions = state.get_assessment(aid)['questions']
    payload = {'type': 'request_hint', 'event_id': 'hint-once'}
    if scope == 'question':
        payload['question_id'] = questions[0]['id']
    elif scope == 'topic':
        payload['topic_id'] = topic['id']
    response = state.add_session_event(session['id'], payload)
    clock[0] = '2026-09-01T10:01:00+00:00'
    restored = factory()
    restored.assessment_service.clock = lambda: clock[0]
    assert restored.add_session_event(session['id'], payload) == response
    stored = restored.assessment_service.repository.get_record('assessments', aid)
    events = stored['snapshot'].get('assistance_events', [])
    assert len(events) == 1
    assert events[0]['kind'] == 'request_hint'
    assert events[0]['delivery_id'] == response['id']
    assert events[0]['delivered_at'] == response['received_at']
    restored.record_attempt(aid, {'answers': [
        {'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    restored.finalize_assessment(aid, {})
    result = restored.assessment_result(aid)['question_results']
    for index, question in enumerate(result):
        expected = scope != 'question' or index == 0
        assert question['assisted'] is expected
        assert question['assistance_reasons'] == ([{'kind': 'request_hint', 'delivered_at': response['received_at']}] if expected else [])


@pytest.mark.parametrize('scope', ['question', 'topic', 'all'])
@pytest.mark.parametrize('revise', [False, True])
def test_session_hint_after_submission_preserves_conservative_independence_policy(workbench, scope, revise):
    _, state, sid, topic, clock = workbench
    state.assessment_service.clock = lambda: clock[0]
    plan = state.create_plan(sid, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    question = state.get_assessment(aid)['questions'][0]
    state.record_attempt(aid, {'answers': [{'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    clock[0] = '2026-09-01T10:01:00+00:00'
    payload = {'type': 'request_hint', 'event_id': 'after-answer'}
    if scope == 'question':
        payload['question_id'] = question['id']
    elif scope == 'topic':
        payload['topic_id'] = topic['id']
    response = state.add_session_event(session['id'], payload)
    repo = state.assessment_service.repository
    stored = repo.get_record('assessments', aid)
    assert stored['questions'][0].get('assisted')
    previous = state.assessment_service._latest(aid)[question['id']]
    assert not previous['assisted']
    from knowpath_backend.learning.assessments.feedback import assistance_reasons
    expected = [{'kind': 'request_hint', 'delivered_at': response['received_at']}]
    assert assistance_reasons(stored, stored['questions'][0], previous) == expected
    if not revise:
        state.finalize_assessment(aid, {'allow_unanswered': True})
        result = state.assessment_result(aid)['question_results'][0]
        assert result['assisted'] and result['assistance_reasons'] == expected
        evidence = next(e for e in repo.records('evidence', assessment_id=aid) if e['question_id'] == question['id'])
        assert not evidence['eligible']
        return
    clock[0] = '2026-09-01T10:02:00+00:00'
    state.record_attempt(aid, {'answers': [{'question_id': question['id'], 'answer': 'B', 'expected_answer_revision': 1}]})
    revised = state.assessment_service._latest(aid)[question['id']]
    assert revised['assisted']
    assert assistance_reasons(repo.get_record('assessments', aid), stored['questions'][0], revised) == [
        {'kind': 'request_hint', 'delivered_at': response['received_at']}]


@pytest.mark.parametrize('scope', ['topic', 'all'])
def test_session_hint_fences_generation_and_rejected_event_rolls_back(workbench, scope):
    from knowpath_backend.learning.errors import DomainConflict
    _, state, sid, topic, _ = workbench
    plan = state.create_plan(sid, {})
    session = state.start_session(plan['id'], plan['tasks'][0]['id'])
    queued = []
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5},
        dispatch=lambda fn, *args: queued.append((fn, args)))['id']
    with pytest.raises(DomainConflict):
        state.add_session_event(session['id'], {'type': 'request_hint', 'topic_id': 'outside', 'event_id': 'invalid'})
    repo = state.assessment_service.repository
    assert not repo.get_record('assessments', aid)['snapshot'].get('assistance_events')
    payload = {'type': 'request_hint', 'event_id': 'during-generation'}
    if scope == 'topic':
        payload['topic_id'] = topic['id']
    response = state.add_session_event(session['id'], payload)
    fn, args = queued[0]
    fn(*args)
    stored = repo.get_record('assessments', aid)
    assert all(q.get('assisted') for q in stored['questions'])
    assert stored['snapshot']['assistance_events'][0]['delivered_at'] == response['received_at']
    state.finish_session(session['id'])
    with pytest.raises(DomainConflict):
        state.add_session_event(session['id'], {'type': 'request_hint', 'event_id': 'finished'})
    assert len(repo.get_record('assessments', aid)['snapshot']['assistance_events']) == 1


@pytest.mark.parametrize('kind', ['diagnostic', 'retest'])
def test_independent_round_withholds_feedback_until_explicit_finalize(workbench, kind):
    _, state, sid, topic, _ = workbench
    created = state.create_assessment(sid, {'kind': kind, 'question_count': 5, 'topic_ids': [topic['id']]})
    aid = created['id']
    public = state.get_assessment(aid)
    assert public['feedback_policy'] == 'after_round'
    for question in public['questions']:
        response = state.record_attempt(aid, {'include_feedback': True, 'answers': [
            {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
        assert response['question_feedback'] == []
        assert state.get_assessment(aid)['question_feedback'] == []
    assert response['next_question_id'] is None
    assert state.get_assessment(aid)['status'] == 'in_progress'
    stored = state.assessment_service.repository.get_record('assessments', aid)
    assert not any(q.get('assisted') for q in stored['questions'])
    assert not stored['snapshot'].get('assistance_events')
    state.finalize_assessment(aid, {})
    result = state.assessment_result(aid)
    assert len(state.get_assessment(aid)['question_feedback']) == 5
    assert result['summary'] == {'correct': 5, 'incorrect': 0, 'unverified': 0, 'independent_evidence_count': 5}
    assert not any(q['assisted'] for q in result['question_results'])


def test_legacy_feedback_policy_is_preserved_and_completed_feedback_protects_other_round(workbench):
    _, state, sid, topic, _ = workbench
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    legacy = state.assessment_service.repository.get_record('assessments', aid)
    legacy['snapshot'].pop('feedback_policy', None)
    state.assessment_service.repository.put_record('assessments', legacy)
    q = state.get_assessment(aid)['questions'][0]
    response = state.record_attempt(aid, {'include_feedback': True, 'answers': [
        {'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    assert len(response['question_feedback']) == 1
    assert 'feedback_policy' not in state.assessment_service.repository.get_record('assessments', aid)['snapshot']


def test_legacy_completed_get_retains_its_disclosed_feedback_set(workbench):
    _, state, sid, _, _ = workbench
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    repo = state.assessment_service.repository
    legacy = repo.get_record('assessments', aid)
    legacy['snapshot'].pop('feedback_policy')
    repo.put_record('assessments', legacy)
    questions = state.get_assessment(aid)['questions']
    state.record_attempt(aid, {'answers': [{'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    state.finalize_assessment(aid, {})
    assert state.get_assessment(aid)['question_feedback'] == []
    other = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    state.assessment_result(aid)
    assert all(q.get('assisted') for q in repo.get_record('assessments', other)['questions'])


def test_after_round_gate_applies_to_both_feedback_exposure_helpers(workbench):
    from knowpath_backend.learning.assessments.feedback import record_feedback_exposure, deliver_feedback_exposure
    _, state, sid, _, _ = workbench
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    service = state.assessment_service
    assessment = service.repository.get_record('assessments', aid)
    assessment['snapshot']['feedback_question_ids'] = [assessment['questions'][0]['id']]
    record_feedback_exposure(assessment, assessment['questions'][:1], {})
    assert not any(q.get('assisted') for q in assessment['questions'])
    service.repository.put_record('assessments', assessment)
    other = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    deliver_feedback_exposure(service, aid)
    assert state.get_assessment(aid)['question_feedback'] == []
    assert not service.repository.get_record('assessments', other)['snapshot'].get('assistance_events')


@pytest.mark.parametrize('endpoint', ['get', 'diagnostic'])
def test_read_uses_the_snapshot_whose_feedback_was_fenced(workbench, monkeypatch, endpoint):
    import knowpath_backend.learning.assessments.service as assessment_module
    _, state, sid, _, _ = workbench
    service = state.assessment_service
    aid = state.create_assessment(sid, {'kind': 'retest', 'adaptive': endpoint == 'diagnostic', 'question_count': 5})['id']
    if endpoint == 'diagnostic':
        while True:
            view = service.diagnostic(aid)
            question = view['current_question']
            if question is None:
                break
            state.record_attempt(aid, {'answers': [
                {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
        assert service.repository.get_record('assessments', aid)['snapshot']['adaptive']['decisions']
    else:
        questions = state.get_assessment(aid)['questions']
        state.record_attempt(aid, {'answers': [
            {'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    other = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    deliver = assessment_module.deliver_feedback_exposure
    def finalize_after_fence(*args, **kwargs):
        snapshot = deliver(*args, **kwargs)
        # The original fence saw an independent round with no disclosures.
        # Complete it before the endpoint would otherwise reload its owner.
        state.finalize_assessment(aid, {})
        return snapshot
    monkeypatch.setattr(assessment_module, 'deliver_feedback_exposure', finalize_after_fence)
    response = getattr(service, endpoint)(aid)
    assert service.repository.get_record('assessments', aid)['status'] == 'completed'
    assert response['status'] == 'in_progress'
    if endpoint == 'get':
        assert response['question_feedback'] == []
    else:
        assert response['decisions'] == [] and response['hypotheses'] == []
    related = service.repository.get_record('assessments', other)
    assert not any(q.get('assisted') for q in related['questions'])
    assert not related['snapshot'].get('assistance_events')
    # A later read fences the completed snapshot before exposing its content.
    monkeypatch.setattr(assessment_module, 'deliver_feedback_exposure', deliver)
    disclosed = getattr(service, endpoint)(aid)
    assert disclosed['status'] == 'completed'
    assert disclosed['question_feedback'] if endpoint == 'get' else disclosed['decisions']
    related = service.repository.get_record('assessments', other)
    assert all(q.get('assisted') for q in related['questions'])
    assert related['snapshot']['assistance_events'][0]['kind'] == 'question_feedback'


def test_completed_round_disclosure_protects_other_active_round_and_is_read_only(workbench):
    _, state, sid, _, _ = workbench
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    questions = state.get_assessment(aid)['questions']
    state.record_attempt(aid, {'answers': [{'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    state.finalize_assessment(aid, {})
    repo = state.assessment_service.repository
    frozen = repo.get_record('assessments', aid)['result']
    other = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    result = state.assessment_result(aid)
    assert repo.get_record('assessments', aid)['result'] == frozen
    assert all(q.get('assisted') for q in repo.get_record('assessments', other)['questions'])
    assert result['current_state']['items'][0]['evidence_sufficiency']['requirements']['minimum_families'] == 3
    assert len(state.get_assessment(aid)['question_feedback']) == 5


def test_adaptive_conclusions_hidden_until_round_completed(workbench):
    _, state, sid, _, _ = workbench
    aid = state.create_assessment(sid, {'adaptive': True, 'question_count': 5})['id']
    view = state.assessment_service.diagnostic(aid)
    assert view['decisions'] == []
    q = view['current_question']
    state.record_attempt(aid, {'include_feedback': True, 'answers': [
        {'question_id': q['id'], 'answer': 'B', 'expected_answer_revision': 0}]})
    view = state.assessment_service.diagnostic(aid)
    assert view['decisions'] == [] and view['hypotheses'] == []
    assert state.assessment_service.repository.get_record('assessments', aid)['snapshot']['adaptive']['decisions']


def test_mixed_result_separates_unverified_without_zero_score(workbench):
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    class Mixed(FixedQuestions):
        def generate(self, topics, payload):
            questions = super().generate(topics, payload)
            for q in questions[-2:]:
                q.update(type='short_answer', options=[], answer_key='Reference explanation')
            return questions
    _, state, sid, _, _ = workbench
    state.assessment_service.generator = Mixed()
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5,
        'question_types': ['single_choice', 'short_answer']})['id']
    questions = state.get_assessment(aid)['questions']
    state.record_attempt(aid, {'answers': [{'question_id': q['id'], 'answer': 'A' if q['type'] == 'single_choice' else 'my explanation', 'expected_answer_revision': 0} for q in questions]})
    state.finalize_assessment(aid, {})
    result = state.assessment_result(aid)
    assert result['summary'] == {'correct': 3, 'incorrect': 0, 'unverified': 2, 'independent_evidence_count': 3}
    assert all(q['score'] is None and q['verdict'] == 'unverified' for q in result['question_results'][-2:])
    assert all(q['reference_answer'] == 'Reference explanation' for q in result['question_results'][-2:])


def test_explanation_projects_internal_option_ids_without_mutating_rubric():
    from knowpath_backend.learning.assessments.feedback import readable_explanation
    question = {'options': [{'id': 'o1', 'text': '十六周岁'}, {'id': 'o10', 'text': '十八周岁'}],
                'rubric': 'o1错误，o10正确。保留source_o1与o100。'}
    assert readable_explanation(question) == '“十六周岁”错误，“十八周岁”正确。保留source_o1与o100。'
    assert question['rubric'].startswith('o1')


def test_feedback_survives_restart_and_replay_without_duplicate_evidence(workbench):
    factory, state, sid, topic, _ = workbench
    assessment = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5, 'topic_ids': [topic['id']]})
    aid = assessment['id']
    question = state.get_assessment(aid)['questions'][0]
    payload = {'include_feedback': True, 'answers': [
        {'question_id': question['id'], 'answer': 'B', 'expected_answer_revision': 0}]}
    submitted = state.record_attempt(aid, payload, idempotency_key='feedback-once')
    restored = factory()
    assert restored.record_attempt(aid, payload, idempotency_key='feedback-once') == submitted
    public = restored.get_assessment(aid)
    assert public['question_feedback'] == submitted['question_feedback']
    assert len(restored.assessment_service.repository.records('attempts', assessment_id=aid)) == 1
    assert restored.assessment_service.repository.records('evidence', assessment_id=aid) == []
    for question in public['questions'][1:]:
        response = restored.record_attempt(aid, {'include_feedback': True, 'answers': [
            {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]},
            idempotency_key=question['id'])
        assert response['question_feedback'][0]['assisted']
        reasons = response['question_feedback'][0]['assistance_reasons']
        assert any(reason['kind'] == 'question_feedback' and reason['delivered_at'] for reason in reasons)
    restored.finalize_assessment(aid, {})
    evidence = restored.assessment_service.repository.records('evidence', assessment_id=aid)
    assert len(evidence) == 5
    assert sum(e['eligible'] for e in evidence) == 1


def test_feedback_marks_related_assessments_but_preserves_previously_submitted_answers(workbench):
    factory, state, sid, topic, _ = workbench
    first = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5, 'topic_ids': [topic['id']]})
    second = state.create_assessment(sid, {'question_count': 5, 'topic_ids': [topic['id']]})
    q2 = state.get_assessment(second['id'])['questions'][0]
    state.record_attempt(second['id'], {'answers': [{'question_id': q2['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    q1 = state.get_assessment(first['id'])['questions'][0]
    state.record_attempt(first['id'], {'include_feedback': True, 'answers': [
        {'question_id': q1['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    related = state.assessment_service.repository.get_record('assessments', second['id'])
    assert not related['questions'][0].get('assisted')
    assert all(q.get('assisted') for q in related['questions'][1:])
    assert related['snapshot']['consulted_sources']
    assert related['snapshot']['assistance_events'][-1]['kind'] == 'question_feedback'
    previous = state.assessment_service._latest(second['id'])[q2['id']]
    assert not previous['assisted']
    state.record_attempt(second['id'], {'answers': [{'question_id': q2['id'], 'answer': 'B', 'expected_answer_revision': 1}]})
    assert state.assessment_service._latest(second['id'])[q2['id']]['assisted']


def test_feedback_fences_inflight_assessment_in_another_space(workbench):
    _, state, sid, topic, _ = workbench
    space = state.get_space(sid)
    other = state.create_space({'name': 'Other', 'material_ids': [space['bindings'][0]['material_id']]})
    queued = []
    pending = state.create_assessment(other['id'], {'question_count': 5},
        dispatch=lambda fn, *args: queued.append((fn, args)))
    first = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5, 'topic_ids': [topic['id']]})
    question = state.get_assessment(first['id'])['questions'][0]
    state.record_attempt(first['id'], {'include_feedback': True, 'answers': [
        {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    fn, args = queued[0]
    fn(*args)
    stored = state.assessment_service.repository.get_record('assessments', pending['id'])
    assert stored['status'] == 'ready'
    assert all(q.get('assisted') for q in stored['questions'])


def test_service_clock_projects_spacing_without_creating_learning_evidence(workbench):
    from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
    _, state, sid, topic, _ = workbench
    service = state.assessment_service
    clock = ['2026-10-01T10:00:00+00:00']
    service.clock = lambda: clock[0]
    aid = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    questions = state.get_assessment(aid)['questions']
    state.record_attempt(aid, {'answers': [{'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    state.finalize_assessment(aid, {})
    first = state.get_state(sid)['items'][0]
    assert first['status'] == 'learning'
    assert first['evidence_sufficiency']['earliest_retest_at'] == '2026-10-02T10:00:00+00:00'
    clock[0] = '2026-10-02T09:59:59+00:00'
    assert state.get_state(sid)['items'][0]['evidence_sufficiency']['can_retest_now'] is False
    clock[0] = '2026-10-02T10:00:00+00:00'
    waiting = state.get_state(sid)['items'][0]
    assert waiting['evidence_sufficiency']['can_retest_now'] is True
    assert waiting['mastery_score'] == 1 and waiting['independent_evidence_count'] == 5
    assert 'insufficient_observation_spacing' in waiting['evidence_sufficiency']['missing']
    assert waiting['status'] != 'mastered'
    class NewFamilies(FixedQuestions):
        def generate(self, topics, payload):
            return [dict(q, prompt=q['prompt'] + ' independent new family') for q in super().generate(topics, payload)]
    service.generator = NewFamilies()
    second = state.create_assessment(sid, {'kind': 'retest', 'question_count': 5})['id']
    questions = state.get_assessment(second)['questions']
    state.record_attempt(second, {'answers': [{'question_id': q['id'], 'answer': 'A', 'expected_answer_revision': 0} for q in questions]})
    state.finalize_assessment(second, {})
    assert state.get_state(sid)['items'][0]['status'] == 'mastered'
