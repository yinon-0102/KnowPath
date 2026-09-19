"""Corrections are durable reviewed graph changes, never fabricated publications."""
import copy
import pytest
from fastapi.testclient import TestClient
from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.errors import DomainConflict, DomainNotFound
from knowpath_backend.test.test_learning_graph_reconciliation import graph_workspace, stage
from knowpath_backend.test.test_learning_graph_worker import worker, event_id


@pytest.fixture
def correction_workspace(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = stage(factory, upload, label)
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged['candidate_revision_id'], {'expected_graph_version': 0}, label+'-publish')
    space = state.create_space({'material_ids': [upload.material.id]}, idempotency_key=label+'-space')
    snapshot = state.graph_service.repository.get_record('graph_revisions', staged['candidate_revision_id'])['publication']['snapshot']
    node = snapshot['nodes'][0]
    payload = {'kind': 'node', 'target_id': node['id'], 'action': 'replace', 'reason': 'Correct heading',
               'source_ref': node['source_refs'][0]['chunk_id'], 'proposed_value': {'name': 'Corrected functions'}}
    try:
        yield factory, upload, label, space, payload
    finally:
        try:
            state.delete_space(space['id'], {'confirm': True, 'expected_version': space['space_version']})
        except DomainNotFound:
            pass


def create(ctx):
    factory, upload, label, space, payload = ctx
    return factory().create_correction(space['id'], payload, idempotency_key=label+'-correction')


def confirm(ctx, correction, **overrides):
    factory, _, label, space, _ = ctx
    return factory().confirm_correction(space['id'], correction['correction_id'],
        {'expected_graph_version': 1, 'reason': 'Reviewed original source', **overrides}, idempotency_key=label+'-confirm')


def test_create_restart_confirm_publish_is_atomic_and_space_stays_pinned(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    assert created['status'] == 'pending'
    assert create(ctx) == created
    state = factory()
    correction = state.graph_service.repository.get_record('corrections', created['correction_id'])
    assert correction['source_ref'] == payload['source_ref']
    assert not state.graph_service.repository.records('outbox', aggregate_id=created['candidate_revision_id'])
    confirmed = confirm(ctx, created)
    assert confirmed['status'] == 'queued'
    assert confirm(ctx, created) == confirmed
    assert worker(factory()).run_once(event_id(state, created))
    restored = factory()
    run = restored.get_run(confirmed['run_id'])
    assert run['status'] == 'succeeded'
    assert run['result_ref']['graph_version'] == 2
    assert run['result_ref']['affected_topic_ids'] == [payload['target_id']]
    assert run['result_ref']['update_available'] is True
    revision = restored.graph_service.repository.get_record('graph_revisions', created['candidate_revision_id'])
    assert revision['publication']['snapshot']['nodes'][0]['name'] == 'Corrected functions'
    assert restored.get_space(space['id'])['bindings'] == space['bindings']
    assert restored.graph_service.repository.get_record('corrections', created['correction_id'])['status'] == 'published'
    assert not worker(restored).run_once(event_id(restored, created))


def test_unconfirmed_correction_cannot_bypass_review(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    with pytest.raises(DomainConflict) as exc:
        factory().graph_service.publish(upload.material.id, created['candidate_revision_id'], {'expected_graph_version': 1}, label+'-bypass')
    assert exc.value.code == 'CORRECTION_CONFIRMATION_REQUIRED'
    staged = factory().graph_service.reconcile(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 1}, label+'-regular')
    assert staged['candidate_revision_id'] != created['candidate_revision_id']


def test_correction_validates_target_source_and_space_ownership(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    state = factory()
    for field in ('target_id', 'source_ref'):
        with pytest.raises((DomainNotFound, DomainConflict)):
            state.create_correction(space['id'], {**payload, field: 'foreign-id'})
    created = create(ctx)
    other = state.create_space({'material_ids': [upload.material.id]})
    try:
        with pytest.raises(DomainNotFound):
            state.confirm_correction(other['id'], created['correction_id'], {'expected_graph_version': 1, 'reason': 'review'})
    finally:
        state.delete_space(other['id'], {'confirm': True, 'expected_version': 1})
    with pytest.raises(DomainConflict) as exc:
        confirm(ctx, created, expected_graph_version=0)
    assert exc.value.code == 'VERSION_CONFLICT'


def test_http_strict_payload_and_idempotency(correction_workspace):
    factory, upload, label, space, payload = correction_workspace
    state = factory()
    client = TestClient(create_app(state.material_service))
    path = '/api/v1/learning-spaces/'+space['id']+'/knowledge-corrections'
    assert client.post(path, json=payload).status_code == 400
    headers = {'Idempotency-Key': label+'-http'}
    for bad in ({**payload, 'kind': 'unknown'}, {**payload, 'proposed_value': {'id': 'overwrite'}}, {k:v for k,v in payload.items() if k!='proposed_value'}):
        assert client.post(path, json=bad, headers=headers).status_code == 422
    result = client.post(path, json=payload, headers=headers)
    assert result.status_code == 201
    assert client.post(path, json=payload, headers=headers).json() == result.json()
    assert client.post(path, json={**payload, 'reason': 'changed'}, headers=headers).status_code == 409
    confirm_path = path+'/'+result.json()['correction_id']+'/confirm'
    confirm_headers = {'Idempotency-Key': label+'-http-confirm'}
    assert client.post(confirm_path, json={'expected_graph_version': True, 'reason': 'review'}, headers=confirm_headers).status_code == 422
    confirmed = client.post(confirm_path, json={'expected_graph_version': 1, 'reason': 'review'}, headers=confirm_headers)
    assert confirmed.status_code == 202
    assert confirmed.json()['status'] == 'queued'
    assert client.post(confirm_path, json={'expected_graph_version': 1, 'reason': 'review'}, headers=confirm_headers).json() == confirmed.json()


def test_rejection_remains_in_audit_but_not_new_space_topics(correction_workspace):
    factory, upload, label, space, payload = correction_workspace
    rejected = {k:v for k,v in payload.items() if k!='proposed_value'}
    rejected['action'] = 'reject'
    ctx = (factory, upload, label, space, rejected)
    created = create(ctx)
    confirm(ctx, created)
    state = factory()
    worker(state).run_once(event_id(state, created))
    revision = state.graph_service.repository.get_record('graph_revisions', created['candidate_revision_id'])
    assert revision['publication']['snapshot']['nodes'][0]['status'] == 'rejected'
    new = state.create_space({'material_ids': [upload.material.id]})
    try:
        assert state.space_service.bound_topics(new) == []
        assert state.space_service.bound_topics(space)
    finally:
        state.delete_space(new['id'], {'confirm': True, 'expected_version': 1})


def test_worker_fails_if_another_revision_wins_publication(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    confirmed = confirm(ctx, created)
    state = factory()
    job = worker(state)
    claimed = job.claim(event_id(state, created))
    staged = state.graph_service.reconcile(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 1}, label+'-competitor')
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged['candidate_revision_id'], {'expected_graph_version': 1}, label+'-winner')
    assert not job.execute(claimed)
    run = factory().get_run(confirmed['run_id'])
    assert run['status'] == 'failed'
    assert run['error']['code'] == 'VERSION_CONFLICT'
    assert state.graph_service.repository.get_record('graph_revisions', created['candidate_revision_id'])['status'] == 'draft'


def test_delete_space_fences_pending_correction(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    confirmed = confirm(ctx, created)
    state = factory()
    job = worker(state)
    identifier = event_id(state, created)
    claimed = job.claim(identifier)
    state.delete_space(space['id'], {'confirm': True, 'expected_version': 1})
    assert not job.execute(claimed)
    with pytest.raises(DomainNotFound):
        factory().graph_service.repository.get_record('corrections', created['correction_id'])
    assert state.graph_service._published(state.graph_service._history(upload.material.id))['graph_version'] == 1


def test_confirmation_concurrency_queues_one_job(correction_workspace):
    from concurrent.futures import ThreadPoolExecutor
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    def submit(index):
        return factory().confirm_correction(space['id'], created['correction_id'],
            {'expected_graph_version': 1, 'reason': 'same review'}, idempotency_key=label+'-parallel-'+str(index))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))
    assert responses[0] == responses[1]
    assert len(factory().graph_service.repository.records('outbox', aggregate_id=created['candidate_revision_id'])) == 1


def test_correction_create_and_confirmation_roll_back_on_replay_storage_failure(correction_workspace, monkeypatch):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    state = factory()
    repo = state.graph_service.repository
    before = repo.records('graph_revisions', material_id=upload.material.id)
    remember = repo.remember
    def fail(*args):
        raise RuntimeError('injected transaction failure')
    monkeypatch.setattr(repo, 'remember', fail)
    with pytest.raises(RuntimeError):
        state.create_correction(space['id'], payload, idempotency_key=label+'-rollback')
    assert repo.records('graph_revisions', material_id=upload.material.id) == before
    assert repo.records('corrections', space_id=space['id']) == []
    monkeypatch.setattr(repo, 'remember', remember)
    created = create(ctx)
    monkeypatch.setattr(repo, 'remember', fail)
    with pytest.raises(RuntimeError):
        state.confirm_correction(space['id'], created['correction_id'], {'expected_graph_version': 1, 'reason': 'review'}, idempotency_key=label+'-rollback-confirm')
    assert repo.records('outbox', aggregate_id=created['candidate_revision_id']) == []
    assert repo.get_record('corrections', created['correction_id'])['status'] == 'pending'


def test_preparation_failure_and_cancellation_never_publish(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    confirmed = confirm(ctx, created)
    class Broken:
        def prepare(self, manifest, heartbeat):
            raise RuntimeError('provider-secret-must-not-leak')
    state = factory()
    assert worker(state, Broken(), max_attempts=1).run_once(event_id(state, created))
    failed = state.get_run(confirmed['run_id'])
    assert failed['status'] == 'failed'
    assert 'provider-secret' not in str(failed)
    assert state.graph_service.repository.get_record('corrections', created['correction_id'])['status'] == 'failed'
    assert state.graph_service._published(state.graph_service._history(upload.material.id))['graph_version'] == 1
    second = state.create_correction(space['id'], payload)
    queued = state.confirm_correction(space['id'], second['correction_id'], {'expected_graph_version': 1, 'reason': 'review'})
    state.cancel_run(queued['run_id'])
    assert not worker(state).run_once(event_id(state, second))
    assert state.get_run(queued['run_id'])['status'] == 'cancelled'


def test_relation_replace_and_reject_rebuild_prerequisites_and_reject_cycles(correction_workspace):
    # The extractor currently emits no relations; seed a source-backed published
    # relation to test the consumer contract independently of future extraction.
    factory, upload, label, space, payload = correction_workspace
    state = factory()
    repo = state.graph_service.repository
    revision = repo.get_record('graph_revisions', space['bindings'][0]['graph_revision_id'])
    snapshot = revision['publication']['snapshot']
    first = snapshot['nodes'][0]
    second = copy.deepcopy(first)
    second.update(id='topic_second', name='Second', prerequisites=[first['id']])
    snapshot['nodes'].append(second)
    snapshot['relations'] = [{'id': 'relation_test', 'from_id': first['id'], 'to_id': second['id'],
        'type': 'prerequisite_of', 'status': 'active', 'source_refs': copy.deepcopy(first['source_refs'])}]
    from knowpath_backend.learning.graph_reconciliation import digest
    revision['publication']['snapshot_hash'] = digest(snapshot)
    with repo.transaction():
        repo.put_record('graph_revisions', revision)
    relation_payload = {**payload, 'kind': 'relation', 'target_id': 'relation_test', 'proposed_value': {'from_id': 'topic_second'}}
    with pytest.raises(DomainConflict) as exc:
        state.create_correction(space['id'], relation_payload)
    assert exc.value.code == 'PREREQUISITE_CYCLE'
    with pytest.raises(DomainConflict) as exc:
        state.create_correction(space['id'], {**relation_payload, 'proposed_value': {'to_id': 'foreign-topic'}})
    assert exc.value.code == 'INVALID_RELATION'
    created = state.create_correction(space['id'], {**relation_payload, 'proposed_value': {'type': 'related_to'}})
    confirmed = state.confirm_correction(space['id'], created['correction_id'], {'expected_graph_version': 1, 'reason': 'review'})
    worker(state).run_once(event_id(state, created))
    result = state.get_run(confirmed['run_id'])
    assert result['status'] == 'succeeded'
    assert set(result['result_ref']['affected_topic_ids']) == {first['id'], second['id']}
    published = repo.get_record('graph_revisions', created['candidate_revision_id'])['publication']['snapshot']
    assert published['relations'][0]['type'] == 'related_to'
    assert published['nodes'][1]['prerequisites'] == []
    with pytest.raises(DomainConflict) as exc:
        state.create_correction(space['id'], payload)
    assert exc.value.code == 'VERSION_CONFLICT'


def test_cancellation_updates_correction_audit(correction_workspace):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    created = create(ctx)
    result = confirm(ctx, created)
    state = factory()
    state.cancel_run(result['run_id'])
    worker(state).run_once(event_id(state, created))
    assert state.graph_service.repository.get_record('corrections', created['correction_id'])['status'] == 'cancelled'


def test_legacy_correction_without_candidate_fails_explicitly(correction_workspace):
    from uuid import uuid4
    from knowpath_backend.learning.spaces import now
    factory, upload, label, space, payload = correction_workspace
    state = factory()
    identifier = str(uuid4())
    with state.graph_service.repository.transaction():
        state.graph_service.repository.put_record('corrections', {'id': identifier, 'space_id': space['id'],
            'kind': 'node', 'target_id': payload['target_id'], 'action': 'reject', 'reason': 'old audit',
            'status': 'pending', 'created_at': now()})
    with pytest.raises(DomainConflict) as exc:
        state.confirm_correction(space['id'], identifier, {'expected_graph_version': 1, 'reason': 'review'})
    assert exc.value.code == 'CORRECTION_NOT_PENDING'


def test_correction_publishes_with_real_graph_and_vector_stores(correction_workspace):
    import os
    if not os.getenv('LEARNING_TEST_NEO4J_URI') or not os.getenv('LEARNING_TEST_QDRANT_URL'):
        pytest.skip('requires explicit Neo4j and Qdrant test endpoints')
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient
    from knowpath_backend.learning.graph_preparation import GraphPreparer, Neo4jGraphBackend
    from knowpath_backend.learning.vector_retrieval import VectorRetriever, QdrantVectorBackend
    from knowpath_backend.test.test_learning_graph_preparation import Embedder
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    state = factory()
    created = create(ctx)
    confirmed = confirm(ctx, created)
    identifier = created['candidate_revision_id']
    driver = GraphDatabase.driver(os.environ['LEARNING_TEST_NEO4J_URI'], auth=(os.environ['NEO4J_USERNAME'], os.environ['NEO4J_PASSWORD']))
    client = QdrantClient(url=os.environ['LEARNING_TEST_QDRANT_URL'], check_compatibility=False)
    backend = QdrantVectorBackend(client, collection_prefix=label)
    preparer = GraphPreparer(state.material_repository, Neo4jGraphBackend(driver), VectorRetriever(Embedder(), backend))
    try:
        assert worker(state, preparer).run_once(event_id(state, created))
        run = factory().get_run(confirmed['run_id'])
        assert run['status'] == 'succeeded'
        assert run['result_ref']['graph_version'] == 2
        revision = state.graph_service.repository.get_record('graph_revisions', identifier)
        assert revision['preparation']['neo4j']['verified'] is True
        assert revision['preparation']['qdrant']['verified'] is True
        assert client.count(backend.collection, exact=True).count == len(upload.version.chunks)
        assert factory().get_space(space['id'])['bindings'] == space['bindings']
    finally:
        if client.collection_exists(backend.collection):
            client.delete_collection(backend.collection)
        with driver.session() as session:
            session.run('MATCH (n) WHERE n.kp_revision_id = $id OR (n:KPRevision AND n.id = $id) DETACH DELETE n', id=identifier).consume()
        preparer.close()


def test_only_active_source_backed_relations_become_learning_prerequisites():
    from knowpath_backend.learning.corrections import validate_relations
    snapshot = {'nodes': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}], 'relations': [
        {'id': 'pending', 'from_id': 'a', 'to_id': 'b', 'type': 'prerequisite_of', 'status': 'pending', 'source_refs': [{'chunk_id': 'source'}]},
        {'id': 'old', 'from_id': 'b', 'to_id': 'c', 'type': 'prerequisite_of', 'status': 'superseded', 'source_refs': [{'chunk_id': 'source'}]},
        {'id': 'no-source', 'from_id': 'a', 'to_id': 'c', 'type': 'prerequisite_of', 'status': 'active', 'source_refs': []},
    ]}
    validate_relations(snapshot)
    assert all(node['prerequisites'] == [] for node in snapshot['nodes'])


@pytest.mark.parametrize('action', ['reject', 'replace'])
def test_same_version_reconcile_preserves_reviewed_correction(correction_workspace, action):
    ctx = correction_workspace
    factory, upload, label, space, payload = ctx
    payload = copy.deepcopy(payload)
    if action == 'reject':
        payload.pop('proposed_value')
        payload['action'] = 'reject'
    ctx = factory, upload, label, space, payload
    created = create(ctx)
    confirm(ctx, created)
    state = factory()
    worker(state).run_once(event_id(state, created))
    repo = state.graph_service.repository
    corrected = repo.get_record('graph_revisions', created['candidate_revision_id'])['publication']['snapshot']
    staged = state.graph_service.reconcile(upload.material.id, {'version_id': upload.version.id, 'expected_graph_version': 2}, label+'-again')
    worker(state).run_once(event_id(state, staged))
    state.graph_service.publish(upload.material.id, staged['candidate_revision_id'], {'expected_graph_version': 2}, label+'-again-publish')
    assert repo.get_record('graph_revisions', staged['candidate_revision_id'])['publication']['snapshot'] == corrected


def test_changed_reviewed_node_metadata_requires_explicit_resolution():
    from knowpath_backend.learning.graph_reconciliation import compare_snapshots
    node = {'id': 'a', 'name': 'Correct name', 'description': 'Correct explanation', 'status': 'rejected', 'content_hash': 'source-hash'}
    base = {'nodes': [node], 'relations': [], 'sources': []}
    candidate = copy.deepcopy(base)
    candidate['nodes'][0].update(name='Original name', description='Original explanation', status='active')
    diff = compare_snapshots(base, candidate)
    assert len(diff['conflicts']) == 1
