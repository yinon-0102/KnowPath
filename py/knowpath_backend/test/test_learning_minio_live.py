"""显式启用的真 MySQL/MinIO 集成测试，仅操作随机创建的测试库和桶。"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from knowpath_backend.learning.materials.raw_migration import migrate_inline
from knowpath_backend.learning.materials.raw_storage import InMemoryRawMaterialStore, MinioRawMaterialStore, ObjectStorageSettings, raw_object_key
from knowpath_backend.learning.persistence.db import Base, MaterialRawRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.state import LearningState
from knowpath_backend.test.test_learning_minio_repository import upload


@pytest.fixture
def mysql_engine():
    url = os.getenv('KNOWPATH_TEST_MYSQL_ADMIN_URL')
    if not url:
        pytest.skip('需要 KNOWPATH_TEST_MYSQL_ADMIN_URL 显式启用隔离 MySQL 测试')
    name = 'knowpath_minio_test_' + uuid4().hex
    admin = create_engine(make_url(url).set(database=None))
    engine = create_engine(make_url(url).set(database=name))
    with admin.begin() as connection:
        connection.execute(text(f'CREATE DATABASE {name}'))
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP DATABASE {name}'))
        admin.dispose()


def test_mysql_migration_and_deletion_keep_object_in_cleanup_journal(mysql_engine):
    store = InMemoryRawMaterialStore()
    repository = SqlAlchemyMaterialRepository(mysql_engine, raw_store=store)
    result = upload(SqlAlchemyMaterialRepository(mysql_engine))
    uploaded, proceed, deleting = threading.Event(), threading.Event(), threading.Event()
    original = store.put
    def paused_put(*args, **kwargs):
        etag = original(*args, **kwargs)
        uploaded.set()
        assert proceed.wait(15)
        return etag
    store.put = paused_put
    state = LearningState(repository)
    def delete():
        deleting.set()
        return state.delete_material(result.material.id, dict(expected_version=1, confirm=True))
    with ThreadPoolExecutor(2) as executor:
        migration = executor.submit(migrate_inline, repository, result.version.id)
        assert uploaded.wait(10)
        deletion = executor.submit(delete)
        assert deleting.wait(5)
        # 等待删除触及资料锁，确保覆盖两个事务交叠的真实 InnoDB 路径。
        import time
        time.sleep(0.3)
        proceed.set()
        assert migration.result(timeout=15) == 'migrated'
        deletion.result(timeout=15)
    event = state.graph_service.repository.records('outbox', event_type='material.delete')[0]
    assert event['payload']['raw_objects'][0]['object_key'] == raw_object_key(result.version.id)
    repository.delete_raw_objects(event['payload']['raw_objects'])
    assert not store.objects


@pytest.fixture
def live_store():
    if os.getenv('KNOWPATH_TEST_MINIO') != '1':
        pytest.skip('需要 KNOWPATH_TEST_MINIO=1 显式启用隔离 MinIO 测试')
    from dataclasses import replace
    store = MinioRawMaterialStore(replace(ObjectStorageSettings.from_env(), bucket='knowpath-test-' + uuid4().hex))
    store._client.make_bucket(store.bucket)
    try:
        yield store
    finally:
        for item in store._client.list_objects(store.bucket, recursive=True, include_version=True):
            store._client.remove_object(store.bucket, item.object_name, version_id=item.version_id)
        store._client.remove_bucket(store.bucket)
        store.close()


def test_real_minio_upload_parse_migrate_delete(mysql_engine, live_store):
    from fastapi.testclient import TestClient
    from knowpath_backend.learning.api import create_app
    from knowpath_backend.learning.materials.service import MaterialService
    from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
    from knowpath_backend.learning.materials.deletion import MaterialDeletionWorker
    repository = SqlAlchemyMaterialRepository(mysql_engine, raw_store=live_store)
    with TestClient(create_app(MaterialService(repository))) as client:
        response = client.post('/api/v1/materials', files={'file': ('live.txt', b'Live MinIO parsing test.')},
                               headers={'Idempotency-Key': 'real-minio'})
        assert response.status_code == 201
        data = response.json()
    restarted = SqlAlchemyMaterialRepository(mysql_engine, raw_store=live_store)
    state = LearningState(restarted)
    with restarted.unit_of_work.session() as session:
        row = session.get(MaterialRawRow, data['version']['id'])
        assert row.content is None and row.storage_backend == 'minio'
    assert MaterialParseWorker(state.graph_service).run_once()
    version = restarted.get_version(data['version']['id'])
    assert version.chunks[0].text == 'Live MinIO parsing test.'
    material = restarted.get_material(version.material_id)
    state.delete_material(material.id, dict(expected_version=material.version, confirm=True))
    class Cleaner:
        def delete(self, payload):
            pass
    assert MaterialDeletionWorker(state.material_deletion_service, Cleaner()).run_once()
    assert live_store.get(raw_object_key(version.id)) is None
    legacy = upload(SqlAlchemyMaterialRepository(mysql_engine), key='legacy', content=b'Legacy bytes')
    assert migrate_inline(restarted, legacy.version.id, prune=True) == 'migrated'
    assert restarted.get_raw(legacy.version.id) == b'Legacy bytes'


def test_real_minio_erases_all_versions(live_store):
    from minio.versioningconfig import VersioningConfig, ENABLED
    live_store._client.set_bucket_versioning(live_store.bucket, VersioningConfig(ENABLED))
    live_store.put('versions', b'first')
    live_store.put('versions', b'second')
    live_store.delete('versions')
    live_store.delete('versions')
    assert list(live_store._client.list_objects(live_store.bucket, recursive=True, include_version=True)) == []
