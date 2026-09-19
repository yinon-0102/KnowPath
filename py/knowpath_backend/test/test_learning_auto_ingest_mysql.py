"""Opt-in MySQL parsing concurrency; only fixture-owned jobs are consumed."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.persistence.db import IdempotencyRow, MaterialRow, RunRow, OutboxEventRow
from knowpath_backend.learning.materials.service import MaterialService
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
from knowpath_backend.learning.workers.graph import GraphWorker
from knowpath_backend.test.test_learning_graph_worker import PreparedBackend


@pytest.fixture
def mysql_upload():
    url = os.getenv('LEARNING_TEST_MYSQL_URL')
    if not url:
        pytest.skip('requires explicit MySQL test URL')
    engine = create_engine(url, pool_pre_ping=True)
    assert engine.dialect.name == 'mysql'
    label = 'auto-ingest-test-' + uuid4().hex
    def factory():
        return create_app(MaterialService(SqlAlchemyMaterialRepository(engine)))
    app = factory()
    try:
        with TestClient(app) as client:
            response = client.post('/api/v1/materials', files={'file': ('notes.txt', label.encode())},
                headers={'Idempotency-Key': label})
            assert response.status_code == 201
        yield factory, response.json(), label
    finally:
        state = app.state.learning_state
        with engine.connect() as connection:
            # The key is authoritative; never delete another material named notes.
            record = connection.execute(select(IdempotencyRow.resource_id).where(IdempotencyRow.key == label)).first()
        if record:
            material_id = record[0]
            with state.graph_service.repository.transaction():
                jobs = [event for event in state.graph_service.repository.records('outbox', lock=False)
                        if event['payload'].get('material_id') == material_id]
                run_ids = {job['payload']['run_id'] for job in jobs}
                state.graph_service.delete_history(material_id)
                state.material_repository.delete_material(material_id)
            with engine.begin() as connection:
                connection.execute(delete(IdempotencyRow).where(IdempotencyRow.key.startswith(label)))
                if run_ids:
                    connection.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))
        engine.dispose()


def parse_event(app, version_id):
    return app.state.learning_state.graph_service.repository.records('outbox', event_type='material.parse', aggregate_id=version_id)[0]


def test_mysql_only_one_worker_claims_and_same_run_reaches_review(mysql_upload):
    factory, data, _ = mysql_upload
    apps = [factory() for _ in range(4)]
    event = parse_event(apps[0], data['version']['id'])
    workers = [MaterialParseWorker(app.state.learning_state.graph_service) for app in apps]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda worker: worker.claim(event['id']), workers))
    assert sum(claim is not None for claim in claims) == 1
    index = next(i for i, claim in enumerate(claims) if claim)
    assert workers[index].execute(claims[index])
    restored = factory().state.learning_state
    history = restored.graph_service._history(data['material']['id'])
    assert len(history) == 1 and history[0]['run_id'] == data['run_id']
    graph_event = restored.graph_service.repository.records('outbox', event_type='graph.prepare', aggregate_id=history[0]['id'])[0]
    assert GraphWorker(restored.graph_service, PreparedBackend()).run_once(graph_event['id'])
    assert restored.get_run(data['run_id'])['status'] == 'succeeded'
    assert restored.graph_service._history(data['material']['id'])[0]['status'] == 'pending_review'


def test_mysql_expired_worker_cannot_commit_after_takeover(mysql_upload):
    factory, data, _ = mysql_upload
    app = factory()
    clock = [datetime.now(timezone.utc)]
    worker = MaterialParseWorker(app.state.learning_state.graph_service, clock=lambda: clock[0], lease_seconds=1)
    event = parse_event(app, data['version']['id'])
    claimed = worker.claim(event['id'])
    clock[0] += timedelta(seconds=2)
    replacement = MaterialParseWorker(factory().state.learning_state.graph_service, clock=lambda: clock[0])
    taken = replacement.claim(event['id'])
    assert taken and not worker.execute(claimed)
    assert replacement.execute(taken)
    assert len(app.state.learning_state.graph_service._history(data['material']['id'])) == 1


def test_mysql_graph_handoff_rollback_leaves_no_sources(mysql_upload, monkeypatch):
    factory, data, _ = mysql_upload
    state = factory().state.learning_state
    repo = state.graph_service.repository
    original = repo.put_record
    def fail(table, row):
        if table == 'outbox' and row['event_type'] == 'graph.prepare':
            raise RuntimeError('sensitive details')
        return original(table, row)
    event = parse_event(factory(), data['version']['id'])
    monkeypatch.setattr(repo, 'put_record', fail)
    assert MaterialParseWorker(state.graph_service, max_attempts=1).run_once(event['id'])
    restored = factory().state.learning_state
    assert restored.get_run(data['run_id'])['status'] == 'failed'
    assert 'sensitive' not in str(restored.get_run(data['run_id']))
    assert not restored.material_repository.get_version(data['version']['id']).chunks
    assert not restored.graph_service._history(data['material']['id'])
