"""Real commands exercise automatic notes without live model/network calls."""
from datetime import datetime, timedelta, timezone

import pytest

from knowpath_backend.test.test_learning_workbench import workbench
from knowpath_backend.test.test_learning_plan_cycles import replan
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.learning.startup import recover_legacy_runs


def completed(state, space_id, *, linked=True, wrong=False, via_attempt=False, before_finalize=None):
    plan = state.create_plan(space_id, {})
    task = plan['tasks'][0]
    links = {'plan_id': plan['id'], 'task_id': task['id']} if linked else {}
    assessment = state.create_assessment(space_id, {
        'kind': 'practice', 'question_count': 5,
        'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}, **links})
    questions = state.get_assessment(assessment['id'])['questions']
    if before_finalize:
        before_finalize(plan, task)
    state.record_attempt(assessment['id'], {'answers': [
        {'question_id': q['id'], 'expected_answer_revision': 0, 'answer': 'B' if wrong else 'A'}
        for q in questions], 'finalize': via_attempt})
    if not via_attempt:
        state.finalize_assessment(assessment['id'], {'allow_unanswered': False})
    return plan, task, assessment


def test_plan_registers_empty_chapters_with_frozen_numbers(workbench):
    factory, state, space_id, _, _ = workbench
    plan = state.create_plan(space_id, {})
    book = state.notes_service.notebook(space_id)
    assert book['chapters'][0]['number'] == '1.1'
    chapter = state.notes_service.chapter(book['chapters'][0]['id'])
    assert chapter['blocks'] == []
    assert chapter['status'] == 'IN_PROGRESS'
    assert factory().notes_service.notebook(space_id)['chapters'][0]['id'] == chapter['id']


@pytest.mark.parametrize('via_attempt', [False, True])
def test_finalize_enqueues_exactly_once_and_returns_note_reference(workbench, via_attempt):
    factory, state, space_id, _, _ = workbench
    _, task, assessment = completed(state, space_id, via_attempt=via_attempt)
    ref = state.assessment_result(assessment['id'])['notes']
    assert ref['chapter_id']
    assert state.get_run(ref['run_id'])['status'] == 'queued'
    assert state.get_assessment(assessment['id'])['status'] == 'completed'
    state.finalize_assessment(assessment['id'], {'allow_unanswered': False})
    events = state.assessment_service.repository.records('outbox', event_type='note.generate')
    assert len(events) == 1
    assert factory().assessment_result(assessment['id'])['notes']['generation_id'] == ref['generation_id']


def test_free_assessment_has_no_note_job(workbench):
    _, state, space_id, _, _ = workbench
    _, _, assessment = completed(state, space_id, linked=False)
    assert state.assessment_result(assessment['id']).get('notes') is None
    assert state.assessment_service.repository.records('outbox', event_type='note.generate') == []


def test_startup_preserves_queued_note_run(workbench):
    _, state, space_id, _, _ = workbench
    _, _, assessment = completed(state, space_id)
    ref = state.assessment_result(assessment['id'])['notes']
    recover_legacy_runs(state.run_service, state.assessment_service.repository)
    assert state.get_run(ref['run_id'])['status'] == 'queued'


def test_worker_consumes_notes_and_failure_leaves_draft(workbench):
    _, state, space_id, _, _ = workbench
    _, _, assessment = completed(state, space_id)
    ref = state.assessment_result(assessment['id'])['notes']
    from knowpath_backend.learning.providers.models import ModelError
    class Unavailable:
        def generate(self, *args):
            raise ModelError('MODEL_UNAVAILABLE')
    state.notes_service.generator = Unavailable()
    worker = ModelTaskWorker(notes=state.notes_service, max_attempts=1)
    assert worker.run_once()
    retry = ModelTaskWorker(notes=state.notes_service, max_attempts=1,
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=30))
    retry.run_once()
    assert state.get_run(ref['run_id'])['status'] == 'failed'
    assert state.get_assessment(assessment['id'])['status'] == 'completed'
    chapter = state.notes_service.chapter(ref['chapter_id'])
    assert chapter['status'] == 'IN_PROGRESS'
    assert chapter['generations'][0]['status'] == 'failed'


def test_space_delete_erases_notes_and_pending_jobs(workbench):
    _, state, space_id, _, _ = workbench
    _, _, assessment = completed(state, space_id)
    state.delete_space(space_id, {'expected_version': state.get_space(space_id)['space_version'], 'confirm': True})
    assert state.notes_service.list_notebooks()['items'] == []
    assert state.assessment_service.repository.records('outbox', event_type='note.generate') == []


class DeterministicNotes:
    def generate(self, snapshot):
        return {'items': [{'evidence_id': e['id'], 'markdown': '已验证的函数知识'}
                          for e in snapshot['evidence']],
                'corrections': [{'evidence_id': e['id'], 'markdown': '据原文纠正本次错误'}
                                for e in snapshot['evidence'] if e['result'] == 'incorrect']}


def test_successful_generation_preserves_edit_and_next_assessment_appends(workbench):
    factory, state, space_id, _, _ = workbench
    later = []
    def prepare_later(plan, task):
        later.append(state.create_assessment(space_id, {'kind': 'practice', 'question_count': 5,
            'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0},
            'plan_id': plan['id'], 'task_id': task['id']}))
    plan, task, assessment = completed(state, space_id, before_finalize=prepare_later)
    ref = state.assessment_result(assessment['id'])['notes']
    state.notes_service.generator = DeterministicNotes()
    chapter = state.notes_service.chapter(ref['chapter_id'])
    state.notes_service.edit(chapter['id'], {'expected_version': chapter['version'],
        'blocks': [{'markdown': '人工补充，不得覆盖'}]})
    assert ModelTaskWorker(notes=state.notes_service).run_once()
    chapter = state.notes_service.chapter(ref['chapter_id'])
    assert state.get_run(ref['run_id'])['status'] == 'succeeded'
    assert any(b['markdown'] == '人工补充，不得覆盖' for b in chapter['blocks'])
    automatic = [b for b in chapter['blocks'] if b['kind'] == 'assessment']
    assert len(automatic) == 1
    assert chapter['status'] == 'IN_PROGRESS'
    # An already linked second assessment completes later and appends to the chapter.
    second = later[0]
    questions = state.get_assessment(second['id'])['questions']
    state.record_attempt(second['id'], {'answers': [
        {'question_id': q['id'], 'expected_answer_revision': 0, 'answer': 'A'} for q in questions]})
    state.finalize_assessment(second['id'], {'allow_unanswered': False})
    assert state.assessment_result(second['id'])['notes']['chapter_id'] == chapter['id']
    assert ModelTaskWorker(notes=state.notes_service).run_once()
    restored = factory().notes_service.chapter(chapter['id'])
    assert len([b for b in restored['blocks'] if b['kind'] == 'assessment']) == 2
    assert any(b['markdown'] == '人工补充，不得覆盖' for b in restored['blocks'])


def test_retry_is_idempotent_and_corrections_are_not_duplicated(workbench):
    _, state, space_id, _, _ = workbench
    _, _, assessment = completed(state, space_id, wrong=True)
    ref = state.assessment_result(assessment['id'])['notes']
    class Invalid:
        def generate(self, snapshot):
            return {'items': [{'evidence_id': 'invented', 'markdown': '不合法'}]}
    state.notes_service.generator = Invalid()
    assert ModelTaskWorker(notes=state.notes_service, max_attempts=1).run_once()
    assert state.get_run(ref['run_id'])['status'] == 'failed'
    retry = state.notes_service.retry(ref['generation_id'], key='retry-once')
    assert state.notes_service.retry(ref['generation_id'], key='retry-once') == retry
    assert retry['run_id'] != ref['run_id']
    state.notes_service.generator = DeterministicNotes()
    assert ModelTaskWorker(notes=state.notes_service).run_once()
    chapter = state.notes_service.chapter(ref['chapter_id'])
    assert len(chapter['corrections']) == 5
    assert len({c['id'] for c in chapter['corrections']}) == 5
    assert state.notes_service.retry(ref['generation_id'], key='retry-once') == retry


def test_index_and_history_lists_do_not_return_un_audited_body(workbench):
    _, state, space_id, _, _ = workbench
    state.create_plan(space_id, {})
    book = state.notes_service.notebook(space_id)
    chapter = state.notes_service.chapter(book['chapters'][0]['id'])
    state.notes_service.edit(chapter['id'], {'expected_version': chapter['version'],
        'blocks': [{'markdown': '只有正文阅读接口可见'}]})
    assert 'blocks' not in state.notes_service.notebook(space_id)['chapters'][0]
    assert all('blocks' not in r for r in state.notes_service.revisions(chapter['id']))
