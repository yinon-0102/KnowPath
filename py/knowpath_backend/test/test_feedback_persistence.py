from knowpath_backend.test.test_learning_workbench import workbench


def test_explanation_projects_internal_option_ids_without_mutating_rubric():
    from knowpath_backend.learning.assessments.feedback import readable_explanation
    question = {'options': [{'id': 'o1', 'text': '十六周岁'}, {'id': 'o10', 'text': '十八周岁'}],
                'rubric': 'o1错误，o10正确。保留source_o1与o100。'}
    assert readable_explanation(question) == '“十六周岁”错误，“十八周岁”正确。保留source_o1与o100。'
    assert question['rubric'].startswith('o1')


def test_feedback_survives_restart_and_replay_without_duplicate_evidence(workbench):
    factory, state, sid, topic, _ = workbench
    assessment = state.create_assessment(sid, {'question_count': 5, 'topic_ids': [topic['id']]})
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
    restored.finalize_assessment(aid, {})
    evidence = restored.assessment_service.repository.records('evidence', assessment_id=aid)
    assert len(evidence) == 5
    assert sum(e['eligible'] for e in evidence) == 1


def test_feedback_marks_related_assessments_but_preserves_previously_submitted_answers(workbench):
    factory, state, sid, topic, _ = workbench
    first = state.create_assessment(sid, {'question_count': 5, 'topic_ids': [topic['id']]})
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
    first = state.create_assessment(sid, {'question_count': 5, 'topic_ids': [topic['id']]})
    question = state.get_assessment(first['id'])['questions'][0]
    state.record_attempt(first['id'], {'include_feedback': True, 'answers': [
        {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]})
    fn, args = queued[0]
    fn(*args)
    stored = state.assessment_service.repository.get_record('assessments', pending['id'])
    assert stored['status'] == 'ready'
    assert all(q.get('assisted') for q in stored['questions'])
