"""旧文件迁移必须先校验对象，再切换引用或清除 SQL 备份。"""
import pytest
from sqlalchemy import create_engine, inspect, text
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pathlib import Path

from knowpath_backend.learning.materials.raw_storage import RawStorageError
from knowpath_backend.learning.materials.raw_migration import migrate_inline, migrate_all
from knowpath_backend.learning.persistence.db import Base, MaterialRawRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.test.test_learning_minio_repository import storage, upload


def test_migration_keeps_sql_backup_until_explicit_prune(storage):
    repo, store, engine = storage
    result = upload(SqlAlchemyMaterialRepository(engine))
    assert migrate_inline(repo, result.version.id) == 'migrated'
    assert repo.get_raw(result.version.id) == b'Original material.'
    with repo.unit_of_work.session() as session:
        assert session.get(MaterialRawRow, result.version.id).content == b'Original material.'
    assert migrate_inline(repo, result.version.id) == 'skipped'
    assert migrate_inline(repo, result.version.id, prune=True) == 'pruned'
    with repo.unit_of_work.session() as session:
        assert session.get(MaterialRawRow, result.version.id).content is None
    assert migrate_inline(repo, result.version.id, prune=True) == 'skipped'
    assert len(store.objects) == 1


def test_migration_dry_run_has_no_side_effects(storage):
    repo, store, engine = storage
    upload(SqlAlchemyMaterialRepository(engine))
    assert migrate_all(repo, apply=False)['pending'] == 1
    assert store.objects == {}
    result = migrate_all(repo, apply=True)
    assert result['migrated'] == 1
    assert migrate_all(repo, apply=True)['migrated'] == 0


def test_readback_failure_preserves_inline_source(storage, monkeypatch):
    repo, store, engine = storage
    result = upload(SqlAlchemyMaterialRepository(engine))
    monkeypatch.setattr(store, 'get', lambda key: b'broken')
    with pytest.raises(RawStorageError):
        migrate_inline(repo, result.version.id, prune=True)
    assert repo.get_raw(result.version.id) == b'Original material.'
    assert store.objects == {}


def test_prune_failure_preserves_backup_and_reference(storage, monkeypatch):
    repo, store, engine = storage
    result = upload(SqlAlchemyMaterialRepository(engine))
    migrate_inline(repo, result.version.id)
    monkeypatch.setattr(store, 'get', lambda key: None)
    with pytest.raises(RawStorageError):
        migrate_inline(repo, result.version.id, prune=True)
    with repo.unit_of_work.session() as session:
        row = session.get(MaterialRawRow, result.version.id)
        assert row.content == b'Original material.'
        assert row.storage_backend == 'minio'


def test_schema_upgrade_preserves_legacy_bytes_and_blocks_unsafe_downgrade(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    url = f'sqlite:///{(tmp_path / "migrate.db").as_posix()}'
    monkeypatch.setenv('DATABASE_URL', url)
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'migrations'))
    engine = create_engine(url)
    try:
        command.upgrade(config, '0014_rag_snapshots')
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO material_raw_files (version_id,content) VALUES ('legacy', :content)"), {'content': b'original'})
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            assert connection.execute(text('SELECT content,storage_backend FROM material_raw_files')).one() == (b'original', 'sql')
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(config, '0014_rag_snapshots')
        assert {c['name'] for c in inspect(engine).get_columns('material_raw_files')} == {'version_id', 'content'}
        command.upgrade(config, 'head')
        with engine.begin() as connection:
            connection.execute(text("UPDATE material_raw_files SET content=NULL, storage_backend='minio'"))
        with pytest.raises(RuntimeError, match='拒绝降级'):
            command.downgrade(config, '0014_rag_snapshots')
    finally:
        engine.dispose()
