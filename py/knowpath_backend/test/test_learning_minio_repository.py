"""验证对象存储与真实 SQL 事务、解析任务和删除任务的配合。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from knowpath_backend.learning.api import create_app
from knowpath_backend.learning.materials.raw_storage import InMemoryRawMaterialStore, RawStorageError, raw_object_key
from knowpath_backend.learning.materials.service import MaterialService
from knowpath_backend.learning.persistence.db import Base, MaterialRawRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
from knowpath_backend.learning.materials.deletion import MaterialDeletionWorker


@pytest.fixture
def storage(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "objects.db"}')
    Base.metadata.create_all(engine)
    store = InMemoryRawMaterialStore()
    repository = SqlAlchemyMaterialRepository(engine, raw_store=store)
    yield repository, store, engine
    engine.dispose()


def upload(repository, key='upload', content=b'Original material.'):
    with repository.transaction():
        return MaterialService(repository).create(filename='notes.txt', content=content,
                                                 idempotency_key=key, defer_parse=True)


def test_new_upload_persists_reference_and_survives_restart(storage):
    repository, store, engine = storage
    result = upload(repository)
    with repository.unit_of_work.session() as session:
        row = session.get(MaterialRawRow, result.version.id)
        assert row.content is None
        assert row.storage_backend == 'minio'
        assert row.object_key == raw_object_key(result.version.id)
        assert row.bucket == store.bucket
        assert row.etag
    restarted = SqlAlchemyMaterialRepository(engine, raw_store=store)
    assert restarted.get_raw(result.version.id) == b'Original material.'
    assert len(store.objects) == 1
    assert upload(repository).replayed
    assert len(store.objects) == 1


def test_legacy_inline_content_remains_readable_when_minio_enabled(storage):
    repository, store, engine = storage
    legacy = upload(SqlAlchemyMaterialRepository(engine))
    assert repository.get_raw(legacy.version.id) == b'Original material.'
    assert store.objects == {}


def test_rollback_removes_new_objects_and_sql_state(storage):
    repository, store, _ = storage
    with pytest.raises(RuntimeError):
        with repository.transaction():
            result = upload(repository)
            raise RuntimeError('abort')
    assert repository.get_version(result.version.id) is None
    assert repository.get_idempotency('upload') is None
    assert store.objects == {}


def test_failed_replay_never_removes_committed_object(storage):
    repository, store, _ = storage
    result = upload(repository)
    with pytest.raises(RuntimeError):
        with repository.transaction():
            upload(repository)
            raise RuntimeError('abort replay')
    assert repository.get_raw(result.version.id) == b'Original material.'
    assert len(store.objects) == 1


def test_lost_commit_receipt_preserves_committed_object(storage):
    from sqlalchemy import event
    repository, store, _ = storage
    with pytest.raises(RuntimeError, match='receipt lost'):
        with repository.transaction():
            result = upload(repository)
            with repository.unit_of_work.session() as session:
                def fail_after_commit(_session):
                    raise RuntimeError('receipt lost')
                event.listen(session, 'after_commit', fail_after_commit, once=True)
    assert repository.get_raw(result.version.id) == b'Original material.'
    assert len(store.objects) == 1


def test_put_failure_rolls_back_sql_and_partial_object(storage, monkeypatch):
    repository, store, _ = storage
    def fail(key, content, **kwargs):
        store.objects[key] = content
        raise RawStorageError('unavailable')
    monkeypatch.setattr(store, 'put', fail)
    with pytest.raises(RawStorageError):
        upload(repository)
    assert repository.list_materials() == []
    assert store.objects == {}


def test_store_failure_returns_safe_503_and_no_material(storage, monkeypatch):
    repository, store, _ = storage
    def fail(*args, **kwargs):
        raise RawStorageError('sensitive backend failure')
    monkeypatch.setattr(store, 'put', fail)
    with TestClient(create_app(MaterialService(repository))) as client:
        response = client.post('/api/v1/materials', files={'file': ('notes.txt', b'Hello')},
                               headers={'Idempotency-Key': 'failed'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'RAW_STORAGE_UNAVAILABLE'
    assert 'sensitive' not in response.text
    assert repository.list_materials() == []


def test_parser_reads_minio_after_new_application_instance(storage):
    repository, store, engine = storage
    with TestClient(create_app(MaterialService(repository))) as client:
        response = client.post('/api/v1/materials', files={'file': ('notes.txt', b'Hello durable object.')},
                               headers={'Idempotency-Key': 'parse'})
        assert response.status_code == 201
        data = response.json()
    state = LearningState(SqlAlchemyMaterialRepository(engine, raw_store=store))
    assert MaterialParseWorker(state.graph_service).run_once()
    version = state.material_repository.get_version(data['version']['id'])
    assert version.chunks[0].text == 'Hello durable object.'
    assert state.run_service.get(data['run_id'])['status'] != 'failed'


def test_immutable_raw_rejects_changed_content(storage):
    repository, _, _ = storage
    result = upload(repository)
    with pytest.raises(ValueError):
        repository.save_raw(result.version.id, b'Changed material.')
    assert repository.get_raw(result.version.id) == b'Original material.'


def test_missing_or_corrupt_object_does_not_fall_back_to_legacy_backup(storage):
    repository, store, _ = storage
    result = upload(repository)
    key = raw_object_key(result.version.id)
    store.objects[key] = b'Corrupted'
    with pytest.raises(RawStorageError):
        repository.get_raw(result.version.id)
    store.objects.pop(key)
    with pytest.raises(RawStorageError):
        repository.get_raw(result.version.id)


def test_missing_store_configuration_does_not_return_empty_data(storage):
    repository, _, engine = storage
    result = upload(repository)
    with pytest.raises(RawStorageError):
        SqlAlchemyMaterialRepository(engine).get_raw(result.version.id)


def test_delete_journals_object_and_retries_before_reporting_success(storage, monkeypatch):
    repository, store, engine = storage
    result = upload(repository)
    state = LearningState(repository)
    deleted = state.delete_material(result.material.id, dict(expected_version=1, confirm=True))
    event = state.graph_service.repository.records('outbox', event_type='material.delete')[0]
    assert event['payload']['raw_objects'][0]['object_key'] == raw_object_key(result.version.id)
    assert repository.get_version(result.version.id) is None
    assert store.objects
    class Cleaner:
        def delete(self, payload):
            pass
    now = datetime.now(timezone.utc)
    worker = MaterialDeletionWorker(state.material_deletion_service, Cleaner(), clock=lambda: now)
    original = store.delete
    def unavailable(key):
        raise RawStorageError('offline')
    monkeypatch.setattr(store, 'delete', unavailable)
    assert worker.run_once(event['id'])
    assert state.run_service.get(deleted['run_id'])['status'] == 'running'
    monkeypatch.setattr(store, 'delete', original)
    now += timedelta(seconds=5)
    assert worker.run_once(event['id'])
    assert not store.objects
    assert state.run_service.get(deleted['run_id'])['status'] == 'succeeded'
