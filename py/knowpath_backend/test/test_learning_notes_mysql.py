"""Real MySQL row locks fence note publication and preserve manual edits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import pytest

from knowpath_backend.test.test_learning_state_persistence import workspace
from knowpath_backend.test.test_learning_note_integration import completed, DeterministicNotes
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker


def mysql(ctx):
    factory, sid, _, engine = ctx
    if engine is None or engine.dialect.name != 'mysql':
        pytest.skip('requires isolated MySQL fixture')
    return factory, sid


def test_mysql_notes_claim_has_one_owner_and_restart_preserves_chapter(workspace):
    factory, sid = mysql(workspace)
    state = factory()
    _, _, assessment = completed(state, sid)
    ref = state.assessment_result(assessment['id'])['notes']
    event = state.assessment_service.repository.records('outbox', aggregate_id=ref['generation_id'])[0]
    instant = datetime.now(timezone.utc)
    def claim(_):
        worker_state = factory()
        worker_state.notes_service.generator = DeterministicNotes()
        worker = ModelTaskWorker(notes=worker_state.notes_service, clock=lambda: instant)
        return worker, worker.claim(event['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    owners = [(w, e) for w, e in results if e]
    assert len(owners) == 1
    owners[0][0].execute(owners[0][1])
    restored = factory()
    assert restored.get_run(ref['run_id'])['status'] == 'succeeded'
    assert len(restored.notes_service.chapter(ref['chapter_id'])['blocks']) == 1


def test_mysql_inflight_generation_appends_after_manual_edit(workspace):
    factory, sid = mysql(workspace)
    state = factory()
    _, _, assessment = completed(state, sid)
    ref = state.assessment_result(assessment['id'])['notes']
    class EditingModel:
        def generate(self, snapshot):
            editor = factory()
            c = editor.notes_service.chapter(ref['chapter_id'])
            editor.notes_service.edit(c['id'], {'expected_version': c['version'],
                'blocks': [{'markdown': '并发人工编辑'}]})
            return DeterministicNotes().generate(snapshot)
    state.notes_service.generator = EditingModel()
    event = state.assessment_service.repository.records('outbox', aggregate_id=ref['generation_id'])[0]
    assert ModelTaskWorker(notes=state.notes_service).run_once(event['id'])
    chapter = factory().notes_service.chapter(ref['chapter_id'])
    assert len(chapter['blocks']) == 2
    assert any(b['markdown'] == '并发人工编辑' for b in chapter['blocks'])
    assert len({b['id'] for b in chapter['blocks']}) == 2
