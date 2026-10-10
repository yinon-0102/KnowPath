"""Acceptance scenarios for real time, assistance, invalidation and erasure."""
import json
from datetime import datetime, timezone

from knowpath_backend.test.test_learning_workbench import workbench
from knowpath_backend.test.test_learning_note_integration import completed, DeterministicNotes
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions
from knowpath_backend.test.test_learning_plan_cycles import save_state
from knowpath_backend.learning.assessments.service import revision


def publish(state, assessment):
    state.notes_service.generator = DeterministicNotes()
    assert ModelTaskWorker(notes=state.notes_service).run_once()
    ref = state.assessment_result(assessment['id'])['notes']
    return state.notes_service.chapter(ref['chapter_id'])


def finish(state, assessment):
    questions = state.get_assessment(assessment['id'])['questions']
    state.record_attempt(assessment['id'], {'answers': [
        {'question_id': q['id'], 'expected_answer_revision': 0, 'answer': 'A'} for q in questions]})
    state.finalize_assessment(assessment['id'], {'allow_unanswered': False})


def test_finished_session_waits_then_uses_real_time(workbench):
    _, state, sid, _, clock = workbench
    plan = state.create_plan(sid, {})
    task = plan['tasks'][0]
    session = state.start_session(plan['id'], task['id'])
    clock[0] = '2026-09-01T10:03:00+00:00'
    state.finish_session(session['id'])
    chapter = state.notes_service.notebook(sid)['chapters'][0]
    assert chapter['generation_status'] == '待总结'
    assert state.notes_service.chapter(chapter['id'])['blocks'] == []
    assessment = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5,
        'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0},
        'learning_session_id': session['id']})
    finish(state, assessment)
    chapter = publish(state, assessment)
    assert chapter['elapsed_seconds'] == 180
    assert '180 秒' in chapter['blocks'][0]['markdown']


def test_read_and_patch_audit_shared_source_active_assessments(workbench):
    _, state, sid, _, _ = workbench
    _, _, original = completed(state, sid)
    chapter = publish(state, original)
    material_ids = [b['material_id'] for b in state.get_space(sid)['bindings']]
    other = state.create_space({'name': '共享资料测评', 'material_ids': material_ids})
    assessment = state.create_assessment(other['id'], {'kind': 'practice', 'question_count': 5,
        'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}})
    repo = state.assessment_service.repository
    assert not any(q.get('assisted') for q in repo.get_record('assessments', assessment['id'])['questions'])
    # PATCH itself returns full automatic body and therefore must mark assistance.
    state.notes_service.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': []})
    assert all(q.get('assisted') for q in repo.get_record('assessments', assessment['id'])['questions'])
    finish(state, assessment)
    assert all(e['assisted'] for e in state.evidence_for(other['id']))


def test_review_preserves_correction_identity_and_reset_removes_qualification(workbench):
    _, state, sid, topic, _ = workbench
    _, _, assessment = completed(state, sid, wrong=True)
    chapter = publish(state, assessment)
    ids = {c['id'] for c in chapter['corrections']}
    state.grade_review(assessment['id'], {'question_id': state.get_assessment(assessment['id'])['questions'][0]['id'],
        'reason': '核查评分'}, idempotency_key='note-grade-review')
    after = state.notes_service.chapter(chapter['id'])
    assert after['correction_count'] == 5
    assert {c['id'] for c in after['corrections']} == ids
    state.reset_state(sid, [topic['id']], '重新学习',
        expected_state_version=state.get_space(sid)['state_version'])
    after = state.notes_service.chapter(chapter['id'])
    assert after['correction_count'] == 0
    assert after['status'] == 'IN_PROGRESS'
    assert not any(c['independent_retest_passed'] for c in after['corrections'])


def test_material_erasure_scrubs_generated_history_and_retains_personal(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid, wrong=True)
    chapter = publish(state, assessment)
    state.notes_service.edit(chapter['id'], {'expected_version': chapter['version'],
        'blocks': [{'markdown': '独立个人段落'}]})
    material = state.get_space(sid)['bindings'][0]['material_id']
    state.delete_material(material, {'expected_version': 1, 'confirm': True, 'cascade': True})
    after = state.notes_service.chapter(chapter['id'])
    assert [b['markdown'] for b in after['blocks']] == ['独立个人段落']
    assert after['corrections'] == []
    assert after['status'] == 'IN_PROGRESS'
    repo = state.assessment_service.repository
    for table in ('note_chapters', 'note_revisions', 'note_generations'):
        assert material not in json.dumps(repo.records(table), ensure_ascii=False)


def test_late_model_result_after_material_delete_is_never_published(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid)
    ref = state.assessment_result(assessment['id'])['notes']
    material = state.get_space(sid)['bindings'][0]['material_id']
    class DeletingModel:
        def generate(self, snapshot):
            output = DeterministicNotes().generate(snapshot)
            state.delete_material(material, {'expected_version': 1, 'confirm': True, 'cascade': True})
            return output
    state.notes_service.generator = DeletingModel()
    assert ModelTaskWorker(notes=state.notes_service).run_once()
    chapter = state.notes_service.chapter(ref['chapter_id'])
    assert chapter['blocks'] == []
    assert state.get_run(ref['run_id'])['status'] == 'cancelled'
    assert all(e['status'] == 'cancelled' for e in state.assessment_service.repository.records('outbox', event_type='note.generate'))


def test_archive_retains_notebook(workbench):
    factory, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid)
    chapter = publish(state, assessment)
    state.update_space(sid, {'status': 'archived', 'expected_version': state.get_space(sid)['space_version']})
    restored = factory().notes_service.chapter(chapter['id'])
    assert restored['blocks']
    assert factory().notes_service.notebook(sid)['chapter_count'] >= 1


def test_all_frozen_topics_current_mastery_required(workbench):
    _, state, sid, topic, _ = workbench
    plan = state.create_plan(sid, {})
    task = plan['tasks'][0]
    state.update_task(plan['id'], task['id'], {'status': 'completed', 'expected_plan_version': plan['version']})
    book = state.notes_service.notebook(sid)
    cid = book['chapters'][0]['id']
    assert state.notes_service.chapter(cid)['status'] == 'IN_PROGRESS'
    save_state(state, sid, topic, status='mastered', mastery_score=1.0,
               next_review_at='2026-10-20T10:00:00+00:00')
    assert state.notes_service.chapter(cid)['status'] == 'COMPLETED'
    # A stale knowledge revision revokes completion even when the raw status remains mastered.
    repo = state.assessment_service.repository
    row = repo.records('states', space_id=sid)[0]
    row['topic_revision_id'] = 'obsolete-revision'
    repo.put_record('states', row)
    assert state.notes_service.chapter(cid)['status'] == 'IN_PROGRESS'
    row['topic_revision_id'] = revision(topic)
    repo.put_record('states', row)
    c = repo.get_record('note_chapters', cid)
    c['topic_ids'].append('unlearned-frozen-topic')
    repo.put_record('note_chapters', c)
    assert state.notes_service.chapter(cid)['status'] == 'IN_PROGRESS'


def test_assisted_questions_do_not_enter_verified_summary(workbench):
    _, state, sid, _, _ = workbench
    def consult(plan, task):
        state.assessment_service.mark_topic_assisted(sid, task['topic_ids'][0])
    _, _, assessment = completed(state, sid, before_finalize=consult)
    chapter = publish(state, assessment)
    assert not chapter['blocks'][0]['items']
    assert chapter['correction_count'] == 0
    assert chapter['status'] == 'IN_PROGRESS'
    assert '暂无独立验证' in chapter['blocks'][0]['markdown']


def test_short_answers_unverified_never_enter_verified_summary(workbench):
    _, state, sid, _, _ = workbench
    class ShortQuestions(FixedQuestions):
        def generate(self, topics, payload):
            questions = super().generate(topics, payload)
            for q in questions:
                q['type'] = 'short_answer'
                q.pop('options', None)
            return questions
    state.assessment_service.generator = ShortQuestions()
    plan = state.create_plan(sid, {})
    assessment = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5,
        'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0},
        'question_types': ['short_answer'], 'plan_id': plan['id'], 'task_id': plan['tasks'][0]['id']})
    finish(state, assessment)
    chapter = publish(state, assessment)
    assert not chapter['blocks'][0]['items']
    assert all(q['verdict'] == 'unverified' for q in state.assessment_result(assessment['id'])['question_results'])
    assert chapter['status'] == 'IN_PROGRESS'
