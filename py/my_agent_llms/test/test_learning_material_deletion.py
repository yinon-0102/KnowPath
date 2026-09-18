"""Material erasure retains unrelated shared records and retries external cleanup."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine
from my_agent_llms.learning.db import init_db
from my_agent_llms.learning.errors import DomainConflict
from my_agent_llms.learning.materials import InMemoryMaterialRepository
from my_agent_llms.learning.repositories import SqlAlchemyMaterialRepository
from my_agent_llms.learning.state import LearningState
from my_agent_llms.test.test_learning_state_persistence import FixedQuestions, create

@pytest.fixture(params=['memory', 'sql'])
def workspace(request, tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'deletion.db'}") if request.param == 'sql' else None
    if engine:
        init_db(engine)
    memory = InMemoryMaterialRepository()
    def factory():
        return LearningState(SqlAlchemyMaterialRepository(engine) if engine else memory, question_generator=FixedQuestions())
    yield factory
    if engine:
        engine.dispose()

def service(state):
    from my_agent_llms.learning.material_deletion import MaterialDeletionService
    return MaterialDeletionService(state.graph_service.repository, state.space_service, state.graph_service, state.run_service)

def seed(state):
    first = state.material_service.create(filename='first.md', content=b'# Functions\n\nReusable behavior.', idempotency_key='first').material
    second = state.material_service.create(filename='second.md', content=b'# Classes\n\nGroup data and methods.', idempotency_key='second').material
    space = state.create_space({'name': 'Shared', 'material_ids': [first.id, second.id]})
    return first, second, space

def delete(state, identifier, cascade=True, version=1):
    return service(state).delete(identifier, {'expected_version': version, 'confirm': True, 'cascade': cascade})

def test_reference_conflict_includes_impacted_spaces_and_no_mutation(workspace):
    state = workspace()
    first, _, space = seed(state)
    with pytest.raises(DomainConflict) as error:
        delete(state, first.id, cascade=False)
    assert error.value.code == 'MATERIAL_IN_USE'
    assert error.value.details['impacted_spaces'] == [{'id': space['id'], 'name': 'Shared'}]
    assert state.material_repository.get_material(first.id)
    assert not state.graph_service.repository.records('outbox', event_type='material.delete')

def test_cascade_erases_versions_questions_evidence_and_keeps_shared_space(workspace):
    state = workspace()
    first, second, space = seed(state)
    repo = state.assessment_service.repository
    topic = next(t for t in state.topics_for_space(space['id']) if t['source_refs'][0]['material_id'] == first.id)
    state.set_scope(space['id'], {'topic_ids': [topic['id']], 'expected_version': 1})
    assessment = create(state, space['id'])
    state.assessment_service.finalize(assessment['id'], {'allow_unanswered': True})
    assert repo.records('evidence', space_id=space['id'])
    response = delete(state, first.id)
    restored = workspace()
    assert response['status'] == 'queued'
    assert restored.get_run(response['run_id'])['status'] == 'queued'
    assert restored.material_repository.get_material(first.id) is None
    assert restored.material_repository.get_raw(first.current_version_id) is None
    assert restored.material_repository.list_versions(first.id) == []
    assert [b['material_id'] for b in restored.space_service.get(space['id'])['bindings']] == [second.id]
    assert restored.material_repository.get_material(second.id)
    assert repo.get_record('assessments', assessment['id'])['questions'] == []
    assert repo.records('evidence', space_id=space['id']) == []
    assert all(s['mastery_score'] is None and s['evidence_count'] == 0 for s in repo.records('states', space_id=space['id']))
    jobs = restored.graph_service.repository.records('outbox', event_type='material.delete')
    assert len(jobs) == 1 and jobs[0]['payload']['run_id'] == response['run_id']

def test_delete_rejects_stale_version_and_strict_confirmation(workspace):
    from pydantic import ValidationError
    state = workspace()
    first, _, _ = seed(state)
    with pytest.raises(DomainConflict) as error:
        delete(state, first.id, version=42)
    assert error.value.code == 'VERSION_CONFLICT'
    for fields in ({}, {'confirm': False}, {'confirm': 'true'}, {'confirm': True, 'cascade': 'true'}):
        with pytest.raises(ValidationError):
            service(state).delete(first.id, {'expected_version': 1, **fields})
    assert state.material_repository.get_material(first.id)

def test_outbox_retry_survives_restart_and_does_not_succeed_on_external_failure(workspace):
    from my_agent_llms.learning.material_deletion import MaterialDeletionWorker
    state = workspace()
    first, _, _ = seed(state)
    response = delete(state, first.id)
    job = state.graph_service.repository.records('outbox', event_type='material.delete')[0]
    calls = []
    class Cleaner:
        def delete(self, payload):
            calls.append(payload['material_id'])
            if len(calls) == 1:
                raise RuntimeError('private provider details')
    timestamp = datetime.now(timezone.utc)
    assert MaterialDeletionWorker(service(state), Cleaner(), clock=lambda: timestamp).run_once(job['id'])
    assert state.graph_service.repository.get_record('outbox', job['id'])['status'] == 'pending'
    assert state.get_run(response['run_id'])['status'] != 'succeeded'
    restored = workspace()
    assert MaterialDeletionWorker(service(restored), Cleaner(), clock=lambda: timestamp + timedelta(seconds=600)).run_once(job['id'])
    assert restored.get_run(response['run_id'])['status'] == 'succeeded'
    assert calls == [first.id, first.id]

def test_failure_rolls_back_material_and_owned_learning_rows(workspace, monkeypatch):
    state = workspace()
    first, _, space = seed(state)
    original = state.graph_service.repository.put_record
    def fail(table, row):
        if table == 'outbox' and row['event_type'] == 'material.delete':
            raise RuntimeError('outbox unavailable')
        return original(table, row)
    monkeypatch.setattr(state.graph_service.repository, 'put_record', fail)
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        delete(state, first.id)
    restored = workspace()
    assert restored.material_repository.get_material(first.id)
    assert len(restored.space_service.get(space['id'])['bindings']) == 2


def test_unrelated_questions_and_evidence_survive_cascade(workspace):
    state = workspace()
    first, second, space = seed(state)
    topic = next(t for t in state.topics_for_space(space['id']) if t['source_refs'][0]['material_id'] == second.id)
    state.set_scope(space['id'], {'topic_ids': [topic['id']], 'expected_version': 1})
    assessment = create(state, space['id'])
    state.assessment_service.finalize(assessment['id'], {'allow_unanswered': True})
    repo = state.assessment_service.repository
    before = repo.records('evidence', space_id=space['id'])
    delete(state, first.id)
    assert len(repo.get_record('assessments', assessment['id'])['questions']) == 5
    assert repo.records('evidence', space_id=space['id']) == before


def test_preparer_cannot_write_after_material_erasure():
    from qdrant_client import QdrantClient
    from my_agent_llms.learning.errors import DomainNotFound
    from my_agent_llms.learning.graph_preparation import GraphPreparer
    from my_agent_llms.learning.graph_worker import preparation_manifest
    from my_agent_llms.learning.vector_retrieval import VectorRetriever, QdrantVectorBackend
    from my_agent_llms.test.test_learning_graph_preparation import Embedder, GraphBackend
    state = LearningState()
    first, _, _ = seed(state)
    staged = state.graph_service.reconcile(first.id, {'version_id': first.current_version_id, 'expected_graph_version': 0}, 'graph')
    manifest = preparation_manifest(state.graph_service.repository.get_record('graph_revisions', staged['candidate_revision_id']))
    graph = GraphBackend()
    client = QdrantClient(':memory:')
    called = []
    def heartbeat():
        if not called:
            called.append(True)
            delete(state, first.id)
    try:
        with pytest.raises(DomainNotFound):
            GraphPreparer(state.material_repository, graph, VectorRetriever(Embedder(), QdrantVectorBackend(client))).prepare(manifest, heartbeat)
        assert not hasattr(graph, 'manifest')
        assert client.get_collections().collections == []
    finally:
        client.close()


def test_external_cleaner_erases_only_target_material_across_profiles():
    from types import SimpleNamespace
    from qdrant_client import QdrantClient, models
    from my_agent_llms.learning.material_deletion import ExternalMaterialCleaner
    from my_agent_llms.learning.vector_retrieval import QdrantVectorBackend
    client = QdrantClient(':memory:')
    backend = QdrantVectorBackend(client, collection_prefix='fixture_erasure')
    alternate = 'fixture_erasure_' + 'f' * 16
    unrelated = 'unrelated_' + 'f' * 16
    historical = 'old_configuration_' + 'a' * 16
    try:
        for collection in (backend.collection, alternate, unrelated, historical):
            client.create_collection(collection, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
            client.upsert(collection, points=[models.PointStruct(id=1, vector=[1.0, 0.0], payload={'material_id': 'target'}),
                                              models.PointStruct(id=2, vector=[0.0, 1.0], payload={'material_id': 'keep'})])
        cleaner = ExternalMaterialCleaner(SimpleNamespace(graph=None, vectors=SimpleNamespace(backend=backend)))
        cleaner.delete({'material_id': 'target', 'revision_ids': [], 'collections': [historical]})
        cleaner.delete({'material_id': 'target', 'revision_ids': []})
        for collection in (backend.collection, alternate, historical):
            assert [p.id for p in client.scroll(collection)[0]] == [2]
        assert client.count(unrelated).count == 2
    finally:
        client.close()


def test_unrelated_completed_results_survive_deleted_snapshot_binding(workspace):
    state = workspace()
    first, second, space = seed(state)
    topic = next(t for t in state.topics_for_space(space['id']) if t['source_refs'][0]['material_id'] == second.id)
    state.set_scope(space['id'], {'topic_ids': [topic['id']], 'expected_version': 1})
    assessment = create(state, space['id'])
    state.assessment_service.finalize(assessment['id'], {'allow_unanswered': True})
    before = state.assessment_service.repository.get_record('assessments', assessment['id'])
    delete(state, first.id)
    after = state.assessment_service.repository.get_record('assessments', assessment['id'])
    assert after['status'] == 'completed'
    assert after['result'] == before['result']


def test_late_question_generator_cannot_restore_erased_sources(workspace):
    state = workspace()
    first, _, space = seed(state)
    class DeleteDuringGeneration(FixedQuestions):
        def generate(self, topics, payload):
            delete(state, first.id)
            return super().generate(topics, payload)
    state.assessment_service.generator = DeleteDuringGeneration()
    assessment = create(state, space['id'])
    assert assessment['questions'] == []
    assert assessment['status'] == 'stale'


def test_partial_preparation_destination_is_durable_for_later_erasure(workspace):
    from types import SimpleNamespace
    from my_agent_llms.learning.graph_worker import GraphWorker
    state = workspace()
    first, _, _ = seed(state)
    staged = state.graph_service.reconcile(first.id, {'version_id': first.current_version_id, 'expected_graph_version': 0}, 'stage')
    collection = 'old_profile_' + 'a' * 16
    class PartialPreparer:
        vectors = SimpleNamespace(backend=SimpleNamespace(collection=collection))
        def prepare(self, manifest, heartbeat):
            raise RuntimeError('failed after some external writes')
    event = state.graph_service.repository.records('outbox', event_type='graph.prepare')[0]
    GraphWorker(state.graph_service, PartialPreparer()).run_once(event['id'])
    delete(state, first.id)
    deletion = workspace().graph_service.repository.records('outbox', event_type='material.delete')[0]
    assert deletion['payload']['revision_ids'] == [staged['candidate_revision_id']]
    assert deletion['payload']['collections'] == [collection]


def test_cancelled_run_still_finishes_irreversible_cleanup(workspace):
    from my_agent_llms.learning.material_deletion import MaterialDeletionWorker
    state = workspace()
    first, _, _ = seed(state)
    response = delete(state, first.id)
    state.run_service.request_cancel(response['run_id'])
    event = state.graph_service.repository.records('outbox', event_type='material.delete')[0]
    calls = []
    class Cleaner:
        def delete(self, payload):
            calls.append(payload['material_id'])
    assert MaterialDeletionWorker(service(state), Cleaner()).run_once(event['id'])
    assert calls == [first.id]
    assert state.graph_service.repository.get_record('outbox', event['id'])['status'] == 'completed'
    assert state.get_run(response['run_id'])['status'] == 'cancelled'


def test_neo4j_cleaner_is_idempotent_and_preserves_another_fixture_revision():
    import os
    from types import SimpleNamespace
    from uuid import uuid4
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient
    from my_agent_llms.learning.graph_preparation import Neo4jGraphBackend
    from my_agent_llms.learning.graph_worker import preparation_manifest
    from my_agent_llms.learning.graph_reconciliation import digest
    from my_agent_llms.learning.material_deletion import ExternalMaterialCleaner
    from my_agent_llms.learning.vector_retrieval import QdrantVectorBackend
    if not os.getenv('LEARNING_TEST_NEO4J_URI'):
        pytest.skip('requires explicit Neo4j test URI')
    ids = [str(uuid4()), str(uuid4())]
    driver = GraphDatabase.driver(os.environ['LEARNING_TEST_NEO4J_URI'], auth=(os.getenv('NEO4J_USERNAME', 'neo4j'), os.environ['NEO4J_PASSWORD']))
    client = QdrantClient(':memory:')
    graph = Neo4jGraphBackend(driver)
    cleaner = ExternalMaterialCleaner(SimpleNamespace(graph=graph, vectors=SimpleNamespace(backend=QdrantVectorBackend(client))))
    try:
        for identifier in ids:
            snapshot = {'nodes': [{'id': 'topic', 'name': 'Fixture', 'source_refs': [{'chunk_id': 'chunk'}]}], 'relations': []}
            manifest = preparation_manifest({'id': identifier, 'snapshot': snapshot, 'snapshot_hash': digest(snapshot), 'base_graph_version': 0, 'diff': {'conflicts': []}})
            assert graph.prepare(manifest, [{'chunk_id': 'chunk', 'material_id': identifier, 'material_version_id': identifier, 'text': 'Fixture source', 'graph_version': 1, 'topic_id': 'topic'}])['verified']
        payload = {'material_id': ids[0], 'revision_ids': [ids[0]]}
        cleaner.delete(payload)
        cleaner.delete(payload)
        with driver.session() as session:
            assert session.run('MATCH (r:KPRevision) WHERE r.id IN $ids RETURN r.id AS id', ids=ids).value() == [ids[1]]
    finally:
        cleaner.delete({'material_id': ids[0], 'revision_ids': ids})
        driver.close()
        client.close()
