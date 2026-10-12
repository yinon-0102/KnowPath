import copy
import json

from knowpath_backend.learning.lessons.adaptation import build_adaptation_context
from knowpath_backend.learning.assessments.service import revision
from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker


AS_OF = "2026-10-12T00:00:00Z"


def evidence(topic_id="t", **changes):
    return {"id": "e", "topic_id": topic_id, "topic_revision_id": "r", "epoch": 0,
            "eligible": True, "assisted": False, "score": 0, "family_id": "family",
            "assessment_id": "a", "created_at": "2026-10-01T00:00:00Z",
            "error_tags": ["concept_confusion"], "answer_key": "SECRET", "rubric": "SECRET", **changes}


def adapt(rows=(), **changes):
    args = {"topics": [{"id": "t", "name": "Target", "learning_revision_id": "r", "source_refs": []},
                       {"id": "p", "name": "Prerequisite", "learning_revision_id": "r", "source_refs": []}],
            "topic_ids": ["t"], "prerequisite_ids": ["p"], "states": {}, "evidence": rows,
            "epochs": {}, "as_of": AS_OF}
    return build_adaptation_context(**{**args, **changes})


def test_effective_error_changes_teaching_and_never_exposes_answer_keys():
    result = adapt([evidence()])
    assert result["topics"][0]["strategy"] == "explain_errors"
    assert result["topics"][0]["error_tags"] == ["concept_confusion"]
    assert "SECRET" not in json.dumps(result)


def test_unrelated_previous_node_assisted_and_old_revision_are_neutral():
    result = adapt([evidence("elsewhere"), evidence(assisted=True), evidence(topic_revision_id="old"), evidence(epoch=1)])
    assert result == adapt()


def test_effective_prerequisite_errors_scaffold_only_necessary_topic():
    result = adapt([evidence("p")])
    assert result["prerequisites"][0]["strategy"] == "scaffold_prerequisite"
    assert result["topics"][0]["strategy"] == "neutral"
    assert adapt([evidence("p")], prerequisite_ids=[])["prerequisites"] == []


def test_disabled_adaptation_is_stable_when_performance_changes():
    assert adapt([evidence()], enabled=False) == adapt(enabled=False)


def add_effective_error(state, sid, topic_id, identifier="adaptation-error"):
    topic = next(t for t in state.space_service.bound_topics(state.space_service.repository.get(sid)) if t['id'] == topic_id)
    row = evidence(topic_id, id=identifier, space_id=sid, topic_revision_id=revision(topic))
    state.lesson_service.repository.put_record('evidence', row)


def test_new_performance_versions_lessons_without_marking_old_sources_stale():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    lessons.generator = Generator()
    first = lessons.create(sid, node, payload)
    ModelTaskWorker(lessons=lessons).run_once()
    old = lessons.get(first['lesson_id'])
    topic_id = old['topic_ids'][0]
    add_effective_error(state, sid, topic_id)
    second = lessons.create(sid, node, payload)
    assert second['lesson_id'] != first['lesson_id']
    ModelTaskWorker(lessons=lessons).run_once()
    current = lessons.get(second['lesson_id'])
    assert current['version'] == old['version'] + 1
    assert current['adaptation_context']['topics'][0]['strategy'] == 'explain_errors'
    assert not lessons.get(first['lesson_id'])['stale']
    assert lessons.get(first['lesson_id'])['sections'] == old['sections']
    assert lessons.list(sid, node)['items'][-1]['adaptation_context'] == current['adaptation_context']
    listed = lessons.list(sid, node)
    assert listed['current_adaptation_fingerprint'] == current['adaptation_fingerprint']
    assert listed['items'][-1]['adaptation_fingerprint'] == current['adaptation_fingerprint']
    assert lessons.get(first['lesson_id'])['current_adaptation_fingerprint'] == current['adaptation_fingerprint']


def test_generation_passes_only_sanitized_adaptation_context_and_keeps_preferences():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    topic_id = lessons._context(sid, node, payload)[1]['topic_ids'][0]
    add_effective_error(state, sid, topic_id)
    class Capture(Generator):
        def generate_json(self, messages):
            prompt = json.loads(messages[-1]['content'])
            assert prompt['adaptation_context']['topics'][0]['strategy'] == 'explain_errors'
            assert 'SECRET' not in messages[-1]['content']
            assert prompt['style'] == 'deep' and prompt['instructions'] == 'more examples'
            assert '核心知识必须完整覆盖本批 covered_topic_ids' in messages[0]['content']
            assert '不得省略目标核心知识' in messages[0]['content']
            return super().generate_json(messages)
    lessons.generator = Capture()
    first = lessons.create(sid, node, {**payload, 'style': 'deep', 'instructions': 'more examples'})
    ModelTaskWorker(lessons=lessons).run_once()
    assert lessons.get(first['lesson_id'])['status'] == 'completed'


def attach_prerequisite(state, sid, payload):
    repository = state.lesson_service.repository
    plan = repository.get_record('plans', payload['plan_id'])
    task = next(t for t in plan['tasks'] if t['id'] == payload['task_id'])
    prerequisite = next(t for t in state.space_service.bound_topics(state.space_service.repository.get(sid)) if t['id'] not in task['topic_ids'])
    task['context']['prerequisites'] = [prerequisite['id']]
    repository.put_record('plans', plan)
    return prerequisite


def test_prerequisite_sources_are_bound_in_scope_and_budgeted():
    state, sid, node, payload = setup_lesson()
    prerequisite = attach_prerequisite(state, sid, payload)
    space, frozen = state.lesson_service._context(sid, node, payload)
    assert frozen['prerequisite_topics'][0]['id'] == prerequisite['id']
    expected = {r['chunk_id'] for r in prerequisite['source_refs']}
    assert expected <= {s['chunk_id'] for s in frozen['sources']}
    bounded = state.lesson_service._bounded(frozen)
    assert prerequisite['id'] in bounded['covered_prerequisite_ids']


def test_out_of_scope_prerequisite_has_no_source_or_teaching_hint():
    state, sid, node, payload = setup_lesson()
    prerequisite = attach_prerequisite(state, sid, payload)
    space = state.space_service.repository.get(sid)
    space['excluded_topic_ids'].append(prerequisite['id'])
    state.space_service.repository.put(space)
    space, frozen = state.lesson_service._context(sid, node, payload)
    assert frozen['prerequisite_topics'] == []
    assert state.lesson_service._adaptation(space, frozen)['prerequisites'] == []


def test_real_source_version_change_marks_existing_lesson_stale():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    first = lessons.create(sid, node, payload)
    lessons.generator = Generator()
    ModelTaskWorker(lessons=lessons).run_once()
    row = lessons.repository.get_record('study_lessons', first['lesson_id'])
    space = state.space_service.repository.get(sid)
    binding = space['bindings'][0]
    updated = state.material_service.create_version(material_id=binding['material_id'], filename='notes.md',
        content=b'# Functions\n\nUpdated functions source.\n\n# Loops\n\nUpdated loops source.', idempotency_key='adaptation-source-update')
    binding['material_version_id'] = updated.version.id
    state.space_service.repository.put(space)
    assert lessons._stale(row) is True


def test_prerequisite_budget_shortfall_does_not_invent_scaffolding_sources(monkeypatch):
    state, sid, node, payload = setup_lesson()
    prerequisite = attach_prerequisite(state, sid, payload)
    add_effective_error(state, sid, prerequisite['id'])
    lessons = state.lesson_service
    frozen = lessons._context(sid, node, payload)[1]
    separate = {**frozen['sources'][0], 'id': 'prerequisite-only', 'chunk_id': 'prerequisite-only', 'text': 'x' * 1000}
    frozen['sources'].append(separate)
    frozen['prerequisite_topics'][0]['source_refs'] = [separate]
    space = state.space_service.repository.get(sid)
    monkeypatch.setattr(lessons, '_context', lambda *args, **kwargs: (space, copy.deepcopy(frozen)))
    main_chunks = {r['chunk_id'] for t in frozen['topics'] for r in t['source_refs']}
    lessons.max_source_chars = sum(len(s['text']) for s in frozen['sources'] if s['chunk_id'] in main_chunks)
    lessons.max_source_batches = 1
    first = lessons.create(sid, node, payload)
    row = lessons.repository.get_record('study_lessons', first['lesson_id'])
    hint = row['snapshot']['adaptation_context']['prerequisites'][0]
    assert prerequisite['id'] in row['snapshot']['uncovered_prerequisite_ids']
    assert hint['source_available'] is False and hint['strategy'] == 'neutral'
    assert sum(len(s['text']) for s in row['snapshot']['sources']) <= lessons.max_source_chars


def test_cancelled_adapted_generation_never_publishes_late_sections():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    topic_id = lessons._context(sid, node, payload)[1]['topic_ids'][0]
    add_effective_error(state, sid, topic_id)
    first = lessons.create(sid, node, payload)
    class Cancel(Generator):
        def generate_json(self, messages):
            result = super().generate_json(messages)
            state.cancel_run(first['run_id'])
            return result
    lessons.generator = Cancel()
    ModelTaskWorker(lessons=lessons).run_once()
    body = lessons.get(first['lesson_id'])
    assert body['status'] == 'cancelled' and body['sections'] == []
    assert not any(e['event'] == 'lesson.section' for e in state.run_service.get(first['run_id'])['events'])


def test_deleting_required_prerequisite_material_scrubs_adapted_lesson():
    state, sid, node, payload = setup_lesson()
    lessons = state.lesson_service
    extra = state.material_service.create(filename='prerequisite.md', content=b'# Basics\n\nA source only for necessary basics.', idempotency_key='adaptation-extra-source')
    space = state.space_service.repository.get(sid)
    space['bindings'].append({'material_id': extra.material.id, 'material_version_id': extra.version.id})
    space['topic_ids'] = []
    state.space_service.repository.put(space)
    prerequisite = next(t for t in state.space_service.bound_topics(space) if t['source_refs'][0]['material_id'] == extra.material.id)
    fresh = state.create_plan(sid, {})
    plan = lessons.repository.get_record('plans', fresh['plan_id'])
    task = next(t for t in plan['tasks'] if prerequisite['id'] not in t['topic_ids'])
    payload = {'plan_id': fresh['plan_id'], 'task_id': task['id']}
    node = task['context']['node_id']
    task['context']['prerequisites'] = [prerequisite['id']]
    lessons.repository.put_record('plans', plan)
    first = lessons.create(sid, node, payload)
    lessons.generator = Generator()
    ModelTaskWorker(lessons=lessons).run_once()
    assert lessons.get(first['lesson_id'])['status'] == 'completed'
    lessons.delete_material(extra.material.id)
    row = lessons.repository.get_record('study_lessons', first['lesson_id'])
    assert row['snapshot'] == {} and row['sections'] == [] and row['citations'] == []
    assert row['status'] == 'cancelled'
