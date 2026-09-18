"""Explicit ingestion must enqueue durable work, never fabricate success."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from my_agent_llms.learning.api import create_app
from my_agent_llms.learning.errors import DomainConflict, DomainNotFound
from my_agent_llms.test.test_learning_graph_reconciliation import graph_workspace
from my_agent_llms.test.test_learning_graph_worker import worker, event_id


def submit(state, upload, key):
    return state.graph_service.ingest(upload.material.id, {"version_id": upload.version.id}, key)


def test_ingest_survives_restart_and_finishes_only_after_preparation(graph_workspace):
    factory, upload, label = graph_workspace
    staged = submit(factory(), upload, label + '-ingest')
    state = factory()
    run = state.get_run(staged['run_id'])
    assert run['kind'] == 'material_ingest'
    assert run['status'] == staged['status'] == 'queued'
    assert submit(state, upload, label + '-ingest') == staged
    assert not any(e['event'] == 'run.completed' for e in run['events'])
    assert worker(state).run_once(event_id(state, staged))
    restored = factory()
    assert restored.get_run(staged['run_id'])['status'] == 'succeeded'
    revision = restored.graph_service.repository.get_record('graph_revisions', staged['candidate_revision_id'])
    assert revision['status'] == 'pending_review'
    assert restored.graph_service._published(restored.graph_service._history(upload.material.id)) is None
    assert submit(restored, upload, label + '-ingest') == staged


def test_ingest_validates_version_ownership_and_persistent_key(graph_workspace):
    factory, upload, label = graph_workspace
    graph = factory().graph_service
    with pytest.raises(DomainNotFound):
        graph.ingest(upload.material.id, {'version_id': 'missing'}, label + '-bad')
    staged = submit(factory(), upload, label + '-ingest')
    with pytest.raises(DomainConflict) as exc:
        graph.ingest(upload.material.id, {'version_id': 'different'}, label + '-ingest')
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert len(graph._history(upload.material.id)) == 1
    assert submit(factory(), upload, label + '-other') == staged


def test_ingest_failure_is_visible_and_new_key_can_retry(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = submit(state, upload, label + '-ingest')
    class Broken:
        def prepare(self, manifest, heartbeat):
            raise RuntimeError('private-provider-credential')
    assert worker(state, Broken(), max_attempts=1).run_once(event_id(state, staged))
    run = state.get_run(staged['run_id'])
    assert run['status'] == 'failed'
    assert 'private-provider' not in str(run)
    assert submit(factory(), upload, label + '-ingest') == staged
    retry = submit(factory(), upload, label + '-retry')
    assert retry['run_id'] != staged['run_id']
    assert worker(factory()).run_once(event_id(state, retry))
    assert factory().get_run(retry['run_id'])['status'] == 'succeeded'


def test_ingest_enqueue_failure_rolls_back_run_revision_and_key(graph_workspace, monkeypatch):
    factory, upload, label = graph_workspace
    state = factory()
    repo = state.graph_service.repository
    original = repo.put_record
    run_ids = []
    def fail_outbox(table, row):
        if table == 'graph_revisions':
            run_ids.append(row['run_id'])
        if table == 'outbox':
            raise RuntimeError('outbox unavailable')
        return original(table, row)
    with monkeypatch.context() as patch:
        patch.setattr(repo, 'put_record', fail_outbox)
        with pytest.raises(RuntimeError, match='outbox unavailable'):
            submit(state, upload, label + '-ingest')
    assert state.graph_service._history(upload.material.id) == []
    for run_id in run_ids:
        with pytest.raises(DomainNotFound):
            state.get_run(run_id)
    assert submit(factory(), upload, label + '-ingest')['status'] == 'queued'


def test_ingest_http_strict_contract(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    with TestClient(create_app(state.material_service, run_service=state.run_service)) as client:
        path = f'/api/v1/materials/{upload.material.id}/ingest'
        headers = {'Idempotency-Key': label + '-http'}
        assert client.post(path, json={'version_id': upload.version.id}).status_code == 400
        for payload in ({}, {'version_id': ''}, {'version_id': 1}, {'version_id': upload.version.id, 'extra': True}):
            assert client.post(path, json=payload, headers=headers).status_code == 422
        assert client.post(path, json={'version_id': 'missing'}, headers=headers).status_code == 404
        response = client.post(path, json={'version_id': upload.version.id}, headers=headers)
        assert response.status_code == 202
        result = response.json()
        assert client.get('/api/v1/runs/' + result['run_id']).json()['status'] == 'queued'
        assert client.post(path, json={'version_id': upload.version.id}, headers=headers).json() == result


def test_concurrent_ingest_coalesces_one_job(graph_workspace):
    factory, upload, label = graph_workspace
    repository = factory().material_repository
    if hasattr(repository, 'unit_of_work') and repository.unit_of_work.engine.dialect.name == 'sqlite':
        pytest.skip('concurrent writer coverage uses MySQL and memory')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: submit(factory(), upload, label + '-concurrent-' + str(i)), range(4)))
    assert len({r['run_id'] for r in results}) == 1
    assert len(factory().graph_service._history(upload.material.id)) == 1


def test_api_restart_keeps_durable_ingestion_but_fails_ephemeral_work(graph_workspace, monkeypatch):
    factory, upload, label = graph_workspace
    state = factory()
    if not hasattr(state.material_repository, 'unit_of_work'):
        pytest.skip('production SQL startup recovery')
    staged = submit(state, upload, label + '-restart')
    ephemeral = state.run_service.create('assessment_generate')
    # Importing the entrypoint must not recover jobs in a configured shared DB.
    monkeypatch.setenv('LEARNING_PERSISTENCE', 'memory')
    import my_agent_llms.learning.main as entrypoint
    import my_agent_llms.learning.db as database
    from my_agent_llms.learning.run_repository import SqlAlchemyRunRepository
    # Exercise production startup, restricted to this fixture's two Runs.
    monkeypatch.setattr(SqlAlchemyRunRepository, 'active_ids',
                        lambda self: [staged['run_id'], ephemeral['id']])
    monkeypatch.setenv('LEARNING_PERSISTENCE', 'sql')
    monkeypatch.setattr(database, 'create_db_engine', lambda: state.material_repository.unit_of_work.engine)
    monkeypatch.setenv('LEARNING_LOCAL_TOKEN', 'test-restart-local-token')
    with TestClient(entrypoint.build_app(), headers={'X-Local-Token': 'test-restart-local-token'}) as client:
        assert client.get('/api/v1/runs/' + staged['run_id']).json()['status'] == 'queued'
        assert client.get('/api/v1/runs/' + ephemeral['id']).json()['status'] == 'failed'
    assert worker(factory()).run_once(event_id(state, staged))
    assert factory().get_run(staged['run_id'])['status'] == 'succeeded'


def test_cancelled_ingest_replay_stays_cancelled_and_new_key_restarts(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    staged = submit(state, upload, label + '-cancel')
    state.cancel_run(staged['run_id'])
    worker(state).run_once(event_id(state, staged))
    assert state.get_run(staged['run_id'])['status'] == 'cancelled'
    assert submit(factory(), upload, label + '-cancel') == staged
    retry = submit(factory(), upload, label + '-restart')
    assert retry['run_id'] != staged['run_id']
    assert worker(factory()).run_once(event_id(state, retry))


def test_ingest_pins_requested_version_and_never_overwrites_publication(graph_workspace):
    factory, upload, label = graph_workspace
    state = factory()
    first = submit(state, upload, label + '-first')
    assert worker(state).run_once(event_id(state, first))
    state.graph_service.publish(upload.material.id, first['candidate_revision_id'],
                               {'expected_graph_version': 0, 'resolutions': []}, label + '-publish')
    second = state.material_service.create_version(material_id=upload.material.id, filename='notes.md',
        content=b'# Functions\n\nChanged content.', idempotency_key=label + '-version')
    pending = submit(state, second, label + '-next')
    assert worker(factory()).run_once(event_id(state, pending))
    published = factory().graph_service._published(factory().graph_service._history(upload.material.id))
    assert published['material_version_id'] == upload.version.id
    revision = state.graph_service.repository.get_record('graph_revisions', pending['candidate_revision_id'])
    assert revision['material_version_id'] == second.version.id
    assert revision['base_graph_version'] == 1
    assert revision['status'] == 'pending_review'
    assert revision['diff']['conflicts']


def test_ingest_retries_entire_transaction_after_outbox_lock_failure(graph_workspace, monkeypatch):
    from sqlalchemy.exc import OperationalError
    factory, upload, label = graph_workspace
    state = factory()
    repo = state.graph_service.repository
    original = repo.put_record
    failed = False
    abandoned_runs = []
    def fail_once(table, row):
        nonlocal failed
        if table == 'outbox' and not failed:
            failed = True
            abandoned_runs.append(row['payload']['run_id'])
            raise OperationalError('outbox insert', {}, Exception(1213, 'simulated deadlock'))
        return original(table, row)
    monkeypatch.setattr(repo, 'put_record', fail_once)
    staged = submit(state, upload, label + '-retry-transaction')
    assert failed
    assert len(repo.records('outbox', aggregate_id=staged['candidate_revision_id'])) == 1
    assert len(state.graph_service._history(upload.material.id)) == 1
    assert staged['run_id'] != abandoned_runs[0]
    with pytest.raises(DomainNotFound):
        state.get_run(abandoned_runs[0])
    assert worker(factory()).run_once(event_id(state, staged))
