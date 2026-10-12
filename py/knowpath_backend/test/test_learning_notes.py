import pytest
from knowpath_backend.test.test_learning_plans_sessions import seed
from knowpath_backend.learning.materials.service import InMemoryMaterialRepository
from knowpath_backend.learning.notes.service import NotesService
from knowpath_backend.test.test_learning_workbench import workbench
from knowpath_backend.test.test_learning_note_integration import completed
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from uuid import uuid4

def notes_fixture():
    state, sid, _ = seed(InMemoryMaterialRepository())
    state.create_plan(sid, {})
    notes = state.notes_service
    return state, sid, notes

def test_register_plan_does_not_invent_learning_and_keeps_numbers():
    state, sid, notes = notes_fixture()
    first = notes.register_plan(sid)
    assert first['chapters']
    assert all(notes.chapter(c['id'])['blocks'] == [] and c['status'] == 'IN_PROGRESS' for c in first['chapters'])
    assert notes.register_plan(sid)['chapters'] == first['chapters']

def test_personal_edit_versions_are_immutable():
    _, sid, notes = notes_fixture()
    chapter = notes.register_plan(sid)['chapters'][0]
    edited = notes.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': [{'kind': 'personal', 'markdown': '我的笔记'}]}, 'edit-one')
    assert edited['blocks'][0]['markdown'] == '我的笔记'
    assert notes.revisions(chapter['id'])
    assert notes.revision(chapter['id'], notes.revisions(chapter['id'])[0]['id'])['blocks'] == []
    with pytest.raises(Exception):
        notes.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': []}, 'edit-two')

def test_successful_generation_preserves_personal_and_has_real_facts(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid, wrong=True)
    ref = state.assessment_result(assessment['id'])['notes']
    notes = state.notes_service
    chapter = notes.chapter(ref['chapter_id'])
    notes.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': [{'markdown': '保留的个人笔记'}]}, 'personal-first')
    class Generator:
        def generate(self, snapshot):
            return {'items': [{'evidence_id': e['id'], 'markdown': '来源支持的知识'} for e in snapshot['evidence']], 'corrections': [{'evidence_id': e['id'], 'markdown': '作答纠正'} for e in snapshot['evidence'] if e['result'] == 'incorrect']}
    notes.generator = Generator()
    assert ModelTaskWorker(notes=notes, max_attempts=1).run_once()
    chapter = notes.chapter(ref['chapter_id'])
    assert chapter['generation_status'] == '已生成'
    assert chapter['blocks'][0]['markdown'] == '保留的个人笔记'
    assert '## 测评结果' in chapter['blocks'][1]['markdown']
    assert '未记录' in chapter['blocks'][1]['markdown']
    assert chapter['correction_count'] > 0
    assert 'blocks' not in notes.notebook(sid)['chapters'][0]
    assert all('blocks' not in r for r in notes.revisions(chapter['id']))

def test_generation_cannot_publish_fabricated_evidence(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid)
    ref = state.assessment_result(assessment['id'])['notes']
    class Generator:
        def generate(self, snapshot): return {'items': [{'evidence_id': 'invented', 'markdown': '虚构'}]}
    state.notes_service.generator = Generator()
    ModelTaskWorker(notes=state.notes_service, max_attempts=1).run_once()
    chapter = state.notes_service.chapter(ref['chapter_id'])
    assert chapter['blocks'] == []
    assert chapter['generations'][0]['error']['code'] == 'MODEL_INVALID_RESPONSE'

def test_edit_response_audits_sources_and_indexes_do_not(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid)
    notes = state.notes_service
    ref = state.assessment_result(assessment['id'])['notes']
    class Generator:
        def generate(self, snapshot): return {'items': [{'evidence_id': e['id'], 'markdown': '来源支持知识'} for e in snapshot['evidence']]}
    notes.generator = Generator()
    ModelTaskWorker(notes=notes, max_attempts=1).run_once()
    active = state.create_assessment(sid, {'kind': 'practice', 'question_count': 5, 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}})
    notes.list_notebooks()
    notes.revisions(ref['chapter_id'])
    raw = state.assessment_service.repository.get_record('assessments', active['id'])
    assert not any(q.get('assisted') for q in raw['questions'])
    chapter = notes.repository.get_record('note_chapters', ref['chapter_id'])
    notes.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': []}, 'audited-edit')
    raw = state.assessment_service.repository.get_record('assessments', active['id'])
    assert any(q.get('assisted') for q in raw['questions'])

def test_material_scrub_cancels_snapshot_and_keeps_personal(workbench):
    _, state, sid, mid, _ = workbench
    _, _, assessment = completed(state, sid)
    notes = state.notes_service
    ref = state.assessment_result(assessment['id'])['notes']
    chapter = notes.chapter(ref['chapter_id'])
    edited = notes.edit(chapter['id'], {'expected_version': chapter['version'], 'blocks': [{'markdown': '个人心得'}]}, 'deletion-personal')
    source = notes.repository.get_record('note_generations', ref['generation_id'])['snapshot']['sources'][0]['material_id']
    notes.delete_material(source)
    row = notes.repository.get_record('note_chapters', chapter['id'])
    assert row['version'] > edited['version']
    assert row['blocks'][0]['markdown'] == '个人心得'
    generation = notes.repository.get_record('note_generations', ref['generation_id'])
    assert generation['snapshot'] == {} and generation['status'] == 'cancelled'
    assert state.get_run(ref['run_id'])['status'] == 'cancelled'

def test_scrub_preserves_unrelated_job_in_same_chapter(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid)
    notes = state.notes_service
    ref = state.assessment_result(assessment['id'])['notes']
    original = notes.repository.get_record('note_generations', ref['generation_id'])
    deleted = original['snapshot']['sources'][0]['material_id']
    other = dict(original, id=str(uuid4()), assessment_id=str(uuid4()), run_id=state.run_service.create('note_generate')['id'], snapshot={'sources': [{'material_id': 'unrelated-B', 'material_version_id': 'B-version', 'chunk_id': 'B-chunk'}]})
    notes.repository.put_record('note_generations', other)
    notes.delete_material(deleted)
    surviving = notes.repository.get_record('note_generations', other['id'])
    assert surviving['status'] == 'generating' and surviving['snapshot'] == other['snapshot']
    assert state.get_run(other['run_id'])['status'] == 'queued'

def test_duplicate_model_corrections_have_stable_unique_identity(workbench):
    _, state, sid, _, _ = workbench
    _, _, assessment = completed(state, sid, wrong=True)
    notes = state.notes_service
    ref = state.assessment_result(assessment['id'])['notes']
    class Generator:
        def generate(self, snapshot):
            e = snapshot['evidence'][0]
            item = {'evidence_id': e['id'], 'markdown': '纠正解释'}
            return {'items': [dict(item), dict(item)], 'corrections': [dict(item), dict(item)]}
    notes.generator = Generator()
    ModelTaskWorker(notes=notes, max_attempts=1).run_once()
    chapter = notes.chapter(ref['chapter_id'])
    assert chapter['correction_count'] == 1
    assert len(chapter['corrections']) == 1
    assert len(chapter['blocks'][0]['items']) == 1

def test_personal_without_locators_resolves_topics_and_audits_new_assessment(workbench, monkeypatch):
    _, state, sid, _, _ = workbench
    plan = state.create_plan(sid, {})
    notes = state.notes_service
    cid = notes.notebook(sid)['chapters'][0]['id']
    chapter = notes.repository.get_record('note_chapters', cid)
    chapter['source_refs'] = []
    chapter['blocks'] = [{'id': 'personal', 'kind': 'personal', 'markdown': '理解记录', 'source_refs': []}]
    notes.repository.put_record('note_chapters', chapter)
    from knowpath_backend.learning.materials.source_access import SourceAccessService
    original = SourceAccessService.prepare_delivery
    created = []
    def create_during_discovery(self, refs, space_id, **kwargs):
        if not created:
            created.append(state.create_assessment(sid, {'kind': 'practice', 'question_count': 5, 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}})['id'])
        return original(self, refs, space_id, **kwargs)
    monkeypatch.setattr(SourceAccessService, 'prepare_delivery', create_during_discovery)
    notes.chapter(cid)
    active = notes.repository.get_record('assessments', created[0])
    assert any(q.get('assisted') for q in active['questions'])
