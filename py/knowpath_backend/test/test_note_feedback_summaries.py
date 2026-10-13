"""Assisted answers still need readable question text in the learning record."""
from knowpath_backend.test.test_learning_notes import notes_fixture
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions


def test_assisted_question_summaries_include_all_answered_questions():
    state, sid, notes = notes_fixture()
    state.assessment_service.generator = FixedQuestions()
    plan = state.plan_sessions._space_plans(sid)[0]
    task = plan['tasks'][0]
    result = state.create_assessment(sid, {'question_count': 5, 'topic_ids': task['topic_ids'],
        'plan_id': plan['id'], 'task_id': task['id']})
    aid = result['id']
    assessment = state.get_assessment(aid)
    for question in assessment['questions']:
        state.record_attempt(aid, {'include_feedback': True, 'answers': [
            {'question_id': question['id'], 'answer': 'A', 'expected_answer_revision': 0}]},
            idempotency_key=question['id'])
    state.finalize_assessment(aid, {})
    ref = state.assessment_result(aid)['notes']
    generation = notes.repository.get_record('note_generations', ref['generation_id'])
    assert len(generation['snapshot']['questions']) == 5
    assert len(generation['snapshot']['evidence']) == 1
    chapter = notes.chapter(ref['chapter_id'])
    assert '题目内容未记录' not in chapter['markdown']
    generation['snapshot']['questions'] = generation['snapshot']['questions'][:1]
    notes.repository.put_record('note_generations', generation)
    assert '题目内容未记录' not in notes.chapter(ref['chapter_id'])['markdown']
    assert len(notes.repository.get_record('note_generations', ref['generation_id'])['snapshot']['questions']) == 1
    # Repair the legacy snapshot through retry before testing model publication.
    generation['snapshot']['questions'] = assessment['questions']
    notes.repository.put_record('note_generations', generation)
    captured = []
    class Generator:
        def generate_json(self, messages):
            import json
            payload = json.loads(messages[-1]['content'])
            captured.append(payload)
            return {'items': [{'evidence_id': e['id'], 'markdown': '来源支持的总结'} for e in payload['evidence']], 'corrections': []}
    notes.generator = Generator()
    from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
    assert ModelTaskWorker(notes=notes, max_attempts=1).run_once()
    assert captured
    payload = captured[0]
    valid = {e['id'] for e in payload['evidence']}
    assert set(payload['allowed_evidence_ids']) == valid
    assert len(payload['questions']) == 1
    assert {r['evidence_id'] for r in payload['assessment_result']['question_results']} == valid
    assert notes.chapter(ref['chapter_id'])['generation_status'] == '已生成'
