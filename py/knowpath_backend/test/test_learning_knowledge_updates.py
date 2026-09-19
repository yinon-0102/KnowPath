"""Published snapshot adoption must preserve history and invalidate changed evidence."""
import copy
import pytest
from fastapi.testclient import TestClient
from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.assessments.service import revision
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.test.test_learning_graph_reconciliation import graph_workspace, stage
from knowpath_backend.test.test_learning_graph_worker import worker, event_id
from knowpath_backend.test.test_learning_state_persistence import FixedQuestions, create, answer


@pytest.fixture
def updates_workspace(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    first = stage(factory, upload, label)
    worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first['candidate_revision_id'], {'expected_graph_version': 0}, label+'-publish')
    space = state.create_space({'material_ids': [upload.material.id]})
    try:
        yield factory, upload, label, space
    finally:
        current = state.space_service.get(space['id'])
        state.delete_space(space['id'], {'confirm': True, 'expected_version': current['space_version']})


def changed(ctx):
    factory, upload, label, space = ctx
    state = factory()
    topic = state.space_service.bound_topics(space)[0]
    correction = state.create_correction(space['id'], {'kind': 'node', 'target_id': topic['id'], 'action': 'replace',
        'reason': 'Review definition', 'source_ref': topic['source_refs'][0]['chunk_id'],
        'proposed_value': {'description': 'Reviewed corrected definition'}}, idempotency_key=label+'-correction')
    state.confirm_correction(space['id'], correction['correction_id'], {'expected_graph_version': 1, 'reason': 'Confirmed'}, idempotency_key=label+'-confirm')
    worker(state).run_once(event_id(state, correction))
    return {'bindings': [{'material_id': upload.material.id, 'material_version_id': upload.version.id, 'graph_version': 2}], 'expected_space_version': 1}


def test_preview_restart_apply_and_replay(updates_workspace):
    ctx = updates_workspace
    factory, upload, label, space = ctx
    payload = changed(ctx)
    state = factory()
    before = state.space_service.get(space['id'])
    preview = state.knowledge_updates(space['id'])
    assert len(preview['available_updates']) == 1
    assert preview['available_updates'][0]['graph_version'] == 2
    assert preview['affected_topic_ids']
    assert state.space_service.get(space['id']) == before
    result = state.apply_knowledge_updates(space['id'], payload, idempotency_key=label+'-apply')
    assert result['space_version'] == 2
    restarted = factory()
    assert restarted.apply_knowledge_updates(space['id'], payload, idempotency_key=label+'-apply') == result
    assert restarted.space_service.get(space['id'])['bindings'][0]['graph_version'] == 2
    assert restarted.knowledge_updates(space['id'])['available_updates'] == []
    run = restarted.get_run(result['run_id'])
    assert run['status'] == 'succeeded'
    assert run['result_ref']['affected_topic_ids'] == result['affected_topic_ids']
    with pytest.raises(DomainConflict):
        restarted.apply_knowledge_updates(space['id'], {**payload, 'expected_space_version': 2}, idempotency_key=label+'-apply')


def test_changed_evidence_stale_and_plan_invalidation_persist(updates_workspace):
    ctx = updates_workspace
    factory, upload, label, space = ctx
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space['id'], label+'-assessment')
    answer(state, assessment, label+'-answer', finalize=True)
    before = state.assessment_service.state(space['id'])['items'][0]
    plan = state.create_plan(space['id'], {})
    payload = changed(ctx)
    preview = state.knowledge_updates(space['id'])
    assert set(preview['invalidated_question_ids']) == {q['id'] for q in assessment['questions']}
    assert plan['id'] in preview['plan_impact']['plan_ids']
    result = state.apply_knowledge_updates(space['id'], payload, idempotency_key=label+'-apply')
    assert result['stale_state_count'] == 1
    restarted = factory()
    saved = restarted.assessment_service.repository.records('states', space_id=space['id'])[0]
    assert saved['score_validity'] == 'stale'
    assert saved['mastery_score'] == before['mastery_score']
    assert saved['evidence_ids'] == before['evidence_ids']
    stored_plan = restarted.assessment_service.repository.get_record('plans', plan['id'])
    assert stored_plan['status'] == 'needs_replan'
    assert stored_plan['version'] == 2
    current_topic = next(t for t in restarted.space_service.bound_topics(restarted.space_service.get(space['id'])) if t['id']==saved['topic_id'])
    assert revision(current_topic) != saved['topic_revision_id']


def test_old_assessment_cannot_restore_changed_score(updates_workspace):
    ctx = updates_workspace
    factory, upload, label, space = ctx
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    old = create(state, space['id'], label+'-old')
    state.apply_knowledge_updates(space['id'], changed(ctx), idempotency_key=label+'-apply')
    answer(state, old, label+'-answer', finalize=True)
    evidence = state.assessment_service.repository.records('evidence', space_id=space['id'])
    assert evidence and all(not e['eligible'] for e in evidence)
    assert state.assessment_service.state(space['id'])['items'] == []
    fresh = create(state, space['id'], label+'-fresh')
    answer(state, fresh, label+'-fresh-answer', finalize=True)
    assert state.assessment_service.state(space['id'])['items'][0]['score_validity'] != 'stale'


def test_unpublished_and_stale_space_are_rejected(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    body = {'bindings': [{'material_id': upload.material.id, 'material_version_id': upload.version.id, 'graph_version': 99}], 'expected_space_version': 1}
    with pytest.raises(DomainConflict):
        state.apply_knowledge_updates(space['id'], body)
    body = changed(updates_workspace)
    with pytest.raises(DomainConflict) as exc:
        state.apply_knowledge_updates(space['id'], {**body, 'expected_space_version': 0})
    assert exc.value.code == 'VERSION_CONFLICT'
    assert state.space_service.get(space['id'])['space_version'] == 1
    foreign = copy.deepcopy(body)
    foreign['bindings'][0]['material_id'] = 'another-material'
    with pytest.raises(DomainConflict) as exc:
        state.apply_knowledge_updates(space['id'], foreign)
    assert exc.value.code == 'BINDING_MISMATCH'
    state.apply_knowledge_updates(space['id'], body)
    old = {'material_id': upload.material.id, 'material_version_id': upload.version.id, 'graph_version': 1}
    with pytest.raises(DomainConflict) as exc:
        state.apply_knowledge_updates(space['id'], {'bindings': [old], 'expected_space_version': 2})
    assert exc.value.code == 'VERSION_CONFLICT'


def test_apply_rolls_back_binding_state_and_run(updates_workspace, monkeypatch):
    factory, upload, label, space = updates_workspace
    body = changed(updates_workspace)
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space['id'], label+'-assessment')
    answer(state, assessment, label+'-answer', finalize=True)
    plan = state.create_plan(space['id'], {})
    before_states = state.assessment_service.repository.records('states', space_id=space['id'])
    before_plan = state.assessment_service.repository.get_record('plans', plan['id'])
    before = state.space_service.get(space['id'])
    rolled_back_runs = []
    original = state.run_service.complete
    def broken(*args, **kwargs):
        rolled_back_runs.append(args[0])
        original(*args, **kwargs)
        raise RuntimeError('transaction rollback probe')
    monkeypatch.setattr(state.run_service, 'complete', broken)
    with pytest.raises(RuntimeError):
        state.apply_knowledge_updates(space['id'], body, idempotency_key=label+'-apply')
    assert factory().space_service.get(space['id']) == before
    assert factory().assessment_service.repository.records('states', space_id=space['id']) == before_states
    assert factory().assessment_service.repository.get_record('plans', plan['id']) == before_plan
    with pytest.raises(DomainNotFound):
        state.get_run(rolled_back_runs[0])
    monkeypatch.setattr(state.run_service, 'complete', original)
    assert state.apply_knowledge_updates(space['id'], body, idempotency_key=label+'-apply')['space_version'] == 2


def test_http_contract_rejects_missing_or_duplicate_bindings(updates_workspace):
    factory, upload, label, space = updates_workspace
    body = changed(updates_workspace)
    with TestClient(create_app(factory().material_service)) as client:
        path = '/api/v1/learning-spaces/'+space['id']+'/knowledge-updates/apply'
        headers = {'Idempotency-Key': label+'-http'}
        for bad in ({'expected_space_version': 1}, {**body, 'bindings': []}, {**body, 'expected_space_version': True}, {**body, 'bindings': body['bindings']*2}):
            assert client.post(path, json=bad, headers=headers).status_code == 422
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 202
        assert client.post(path, json=body, headers=headers).json() == response.json()


def publish_next(ctx, content=None):
    factory, upload, label, space = ctx
    state = factory()
    version = upload.version
    if content is not None:
        version = state.material_service.create_version(material_id=upload.material.id, filename='next.md', content=content,
                                                       idempotency_key=label+'-version2').version
    staged = state.graph_service.reconcile(upload.material.id, {'version_id': version.id, 'expected_graph_version': 1}, label+'-next')
    worker(state).run_once(event_id(state, staged))
    diff = state.graph_service.diff(upload.material.id, staged['candidate_revision_id'])
    resolutions = [{'conflict_id': c['conflict_id'], 'action': 'use_new', 'reason': 'Review new source'} for c in diff['conflicts']]
    state.graph_service.publish(upload.material.id, staged['candidate_revision_id'], {'expected_graph_version': 1, 'resolutions': resolutions}, label+'-next-publish')
    return {'bindings': [{'material_id': upload.material.id, 'material_version_id': version.id, 'graph_version': 2}], 'expected_space_version': 1}


def test_identical_graph_preserves_evidence_and_noop_does_not_increment(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    old = create(state, space['id'], label+'-assessment')
    answer(state, old, label+'-answer', finalize=True)
    before = state.assessment_service.state(space['id'])
    body = publish_next(updates_workspace)
    assert state.knowledge_updates(space['id'])['affected_topic_ids'] == []
    response = state.apply_knowledge_updates(space['id'], body)
    assert response['stale_state_count'] == 0
    assert factory().assessment_service.state(space['id']) == before
    with pytest.raises(DomainConflict) as exc:
        state.apply_knowledge_updates(space['id'], {**body, 'expected_space_version': 2})
    assert exc.value.code == 'NO_KNOWLEDGE_UPDATES'
    assert state.space_service.get(space['id'])['space_version'] == 2


def test_cross_material_version_reuses_unchanged_topic_evidence(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    old = create(state, space['id'], label+'-assessment')
    answer(state, old, label+'-answer', finalize=True)
    before = state.assessment_service.state(space['id'])['items'][0]
    content = ('# Functions\n\nReusable functions ' + label + '\n\n# Parameters\n\nNamed inputs.').encode()
    body = publish_next(updates_workspace, content)
    state.apply_knowledge_updates(space['id'], body)
    current = factory().assessment_service.state(space['id'])['items'][0]
    assert current == before
    assert current['topic_id'] not in state.knowledge_update_service._impact(space, state.space_service.get(space['id'])['bindings'])[2]


def test_generation_after_adoption_is_stale_and_retains_history(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    pending = []
    created = state.create_assessment(space['id'], {'question_count': 5, 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}},
                                     idempotency_key=label+'-assessment', dispatch=lambda fn, aid: pending.append((fn, aid)))
    state.apply_knowledge_updates(space['id'], changed(updates_workspace))
    pending[0][0](pending[0][1])
    run = state.get_run(created['run_id'])
    assert run['status'] == 'failed'
    assert run['error']['code'] == 'STALE_INPUT'
    stored = state.assessment_service.repository.get_record('assessments', created['id'])
    assert stored['status'] == 'stale'
    assert len(stored['questions']) == 5


def test_reject_adoption_keeps_explicit_scope_and_old_history(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    topic = state.space_service.bound_topics(space)[0]
    state.set_scope(space['id'], {'topic_ids': [topic['id']], 'expected_version': 1})
    created = state.create_correction(space['id'], {'kind': 'node', 'target_id': topic['id'], 'action': 'reject',
        'reason': 'Invalid statement', 'source_ref': topic['source_refs'][0]['chunk_id']})
    state.confirm_correction(space['id'], created['correction_id'], {'expected_graph_version': 1, 'reason': 'Reviewed'})
    worker(state).run_once(event_id(state, created))
    body = {'bindings': [{'material_id': upload.material.id, 'material_version_id': upload.version.id, 'graph_version': 2}], 'expected_space_version': 2}
    state.apply_knowledge_updates(space['id'], body)
    updated = state.space_service.get(space['id'])
    assert updated['topic_ids'] == [topic['id']]
    assert state.assessment_service._topics(updated) == []
    assert state.graph_service.repository.get_record('graph_revisions', space['bindings'][0]['graph_revision_id'])['publication']['snapshot']['nodes'][0]['status'] != 'rejected'


def test_mysql_concurrent_apply_same_key_commits_once(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    if not getattr(state.material_repository, 'unit_of_work', None) or state.material_repository.unit_of_work.engine.dialect.name != 'mysql':
        pytest.skip('real MySQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    body = changed(updates_workspace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: factory().apply_knowledge_updates(space['id'], body, idempotency_key=label+'-concurrent'), range(2)))
    assert replies[0] == replies[1]
    assert state.space_service.get(space['id'])['space_version'] == 2


def test_mysql_preview_does_not_wait_for_assessment_writer(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    uow = getattr(state.material_repository, 'unit_of_work', None)
    if not uow or uow.engine.dialect.name != 'mysql':
        pytest.skip('real MySQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import text
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space['id'], label+'-assessment')
    locked, release = Event(), Event()
    def writer():
        other = factory()
        with other.assessment_service.repository.transaction():
            other.assessment_service.repository.get_record('assessments', assessment['id'])
            locked.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert locked.wait(5)
            with uow.transaction():
                session = uow._current.get()
                old_timeout = session.scalar(text('SELECT @@SESSION.innodb_lock_wait_timeout'))
                session.execute(text('SET SESSION innodb_lock_wait_timeout = 1'))
                try:
                    result = state.knowledge_updates(space['id'])
                    assert result['available_updates'] == []
                finally:
                    session.execute(text(f'SET SESSION innodb_lock_wait_timeout = {int(old_timeout)}'))
        finally:
            release.set()
            future.result(timeout=10)


def test_local_replan_retests_changed_completed_topic_and_keeps_history(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    plan = state.create_plan(space['id'], {})
    task = plan['tasks'][0]
    state.update_task(plan['id'], task['id'], {'status': 'completed', 'expected_plan_version': 1})
    state.apply_knowledge_updates(space['id'], changed(updates_workspace))
    latest = factory().get_plan(plan['id'])
    rebuilt = state.create_plan(space['id'], {'rebuild_mode': 'local_replan', 'base_plan_id': plan['id'],
        'expected_plan_version': latest['version'], 'include_review': False})
    relevant = [t for t in rebuilt['tasks'] if t['topic_ids'] == task['topic_ids']]
    assert any(t['status'] == 'pending' and not t['context'].get('historical') for t in relevant)
    assert any(t['status'] == 'completed' and t['context'].get('historical') for t in relevant)
    assert state.get_run(rebuilt['run_id'])['status'] == 'succeeded'


def test_stale_score_requires_retest_even_when_optional_review_disabled(updates_workspace):
    factory, upload, label, space = updates_workspace
    state = factory()
    state.assessment_service.generator = FixedQuestions()
    assessment = create(state, space['id'], label+'-assessment')
    answer(state, assessment, label+'-answer', finalize=True)
    state.apply_knowledge_updates(space['id'], changed(updates_workspace))
    plan = state.create_plan(space['id'], {'include_review': False})
    topic_id = state.assessment_service.state(space['id'])['items'][0]['topic_id']
    assert any(t['topic_ids'] == [topic_id] and t['kind'] == 'diagnostic' for t in plan['tasks'])


def test_mysql_generation_completion_uses_assessment_then_space_lock(updates_workspace, monkeypatch):
    factory, upload, label, space = updates_workspace
    state = factory()
    uow = getattr(state.material_repository, 'unit_of_work', None)
    if not uow or uow.engine.dialect.name != 'mysql':
        pytest.skip('real MySQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    state.assessment_service.generator = FixedQuestions()
    pending = []
    created = state.create_assessment(space['id'], {'question_count': 5, 'difficulty_mix': {'easy': 1.0, 'medium': 0.0, 'hard': 0.0}},
                                     idempotency_key=label+'-assessment', dispatch=lambda fn, aid: pending.append((fn, aid)))
    locked, completing = Event(), Event()
    original = state.assessment_service.repository.get_record
    def observed(table, identifier, **kwargs):
        if table == 'assessments' and uow.active:
            completing.set()
        return original(table, identifier, **kwargs)
    monkeypatch.setattr(state.assessment_service.repository, 'get_record', observed)
    def sender_locks():
        other = factory()
        with other.assessment_service.repository.transaction():
            other.assessment_service.repository.get_record('assessments', created['id'])
            locked.set()
            assert completing.wait(5)
            other.space_service.repository.get(space['id'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sender_locks)
        assert locked.wait(5)
        try:
            pending[0][0](pending[0][1])
        finally:
            future.result(timeout=10)
    assert state.get_run(created['run_id'])['status'] == 'succeeded'
    assert state.get_assessment(created['id'])['status'] == 'ready'
