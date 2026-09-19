import hashlib
import io
from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from knowpath_backend.learning import materials
from knowpath_backend.learning.db import Base
from knowpath_backend.learning.materials import (
    IdempotencyConflict, InMemoryMaterialRepository, MaterialParser,
    MaterialParseError, MaterialService, UnsupportedMaterial,
)
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository


@pytest.fixture(params=['memory', 'sqlite'])
def repository(request, tmp_path):
    if request.param == 'memory':
        return InMemoryMaterialRepository()
    engine = create_engine(f'sqlite:///{tmp_path / "raw.db"}')
    Base.metadata.create_all(engine)
    return SqlAlchemyMaterialRepository(engine)


def create(service, **kwargs):
    return service.create(filename='notes.txt', content=b'original text', idempotency_key='first', **kwargs)


def test_deferred_upload_stores_raw_without_parsing(repository):
    parser = Mock(spec=MaterialParser)
    result = create(MaterialService(repository, parser), defer_parse=True)
    parser.parse.assert_not_called()
    assert result.material.status == result.version.status == 'uploaded'
    assert result.version.chunks == []
    assert repository.get_raw(result.version.id) == b'original text'
    assert b'original text' not in asdict(result.version).values()


def test_raw_is_immutable_and_survives_repository_restart(repository):
    result = create(MaterialService(repository))
    repository.save_raw(result.version.id, b'original text')
    with pytest.raises(ValueError):
        repository.save_raw(result.version.id, b'changed')
    if isinstance(repository, SqlAlchemyMaterialRepository):
        engine = repository.unit_of_work.engine
        database_url = engine.url
        engine.dispose()
        repository = SqlAlchemyMaterialRepository(create_engine(database_url))
    assert repository.get_raw(result.version.id) == b'original text'
    assert repository.get_raw('missing') is None


def test_upload_raw_and_idempotency_roll_back_together(repository):
    with pytest.raises(RuntimeError):
        with repository.transaction():
            result = create(MaterialService(repository), defer_parse=True)
            raise RuntimeError('abort')
    assert repository.get_material(result.material.id) is None
    assert repository.get_version(result.version.id) is None
    assert repository.get_raw(result.version.id) is None
    assert repository.get_idempotency('first') is None


def test_material_deletion_removes_raw_even_without_sqlite_foreign_keys(repository):
    result = create(MaterialService(repository))
    repository.delete_material(result.material.id)
    assert repository.get_raw(result.version.id) is None


def test_version_updates_replace_chunks_and_status_atomically(repository):
    result = create(MaterialService(repository), defer_parse=True)
    parsed = replace(result.version, status='ready', chunks=MaterialParser().parse(b'parsed', filename='notes.txt'))
    with pytest.raises(RuntimeError):
        with repository.transaction():
            repository.update_version(parsed)
            raise RuntimeError('abort')
    assert repository.get_version(parsed.id).status == 'uploaded'
    assert repository.get_version(parsed.id).chunks == []
    repository.update_version(parsed)
    saved = repository.get_version(parsed.id)
    assert saved.status == 'ready'
    assert [chunk.text for chunk in saved.chunks] == ['parsed']
    assert repository.get_raw(parsed.id) == b'original text'


@pytest.mark.parametrize('replay_key', ['first', 'duplicate'])
def test_legacy_replays_backfill_missing_raw(repository, replay_key):
    service = MaterialService(repository)
    result = create(service)
    if isinstance(repository, InMemoryMaterialRepository):
        repository.raw_files.clear()
    else:
        from knowpath_backend.learning.db import MaterialRawRow
        with repository.unit_of_work.session() as session:
            session.query(MaterialRawRow).delete()
    replay = service.create(filename='notes.txt', content=b'original text', idempotency_key=replay_key)
    assert replay.replayed
    assert repository.get_raw(result.version.id) == b'original text'


def test_nondefault_flags_and_version_note_are_part_of_idempotency(repository):
    service = MaterialService(repository)
    first = create(service, defer_parse=True, auto_ingest=False)
    with pytest.raises(IdempotencyConflict):
        create(service, defer_parse=True, auto_ingest=True)
    options = dict(material_id=first.material.id, filename='next.txt', content=b'next', idempotency_key='version', defer_parse=True, auto_ingest=False)
    version = service.create_version(**options, change_note='first note')
    assert version.version.status == 'uploaded'
    with pytest.raises(IdempotencyConflict):
        service.create_version(**options, change_note='different note')


def test_default_fingerprint_is_backward_compatible(repository):
    result = create(MaterialService(repository))
    content_hash = hashlib.sha256(b'original text').hexdigest()
    expected = hashlib.sha256(f'notes.txt\0\0{content_hash}'.encode()).hexdigest()
    assert repository.get_idempotency('first')[0] == expected
    version = MaterialService(repository).create_version(material_id=result.material.id, filename='next.txt', content=b'next', idempotency_key='next')
    expected = hashlib.sha256(f'{result.material.id}\0next.txt\0{version.version.content_hash}'.encode()).hexdigest()
    assert repository.get_idempotency('next')[0] == expected


def test_twenty_mib_limit_is_inclusive_and_checked_before_parser():
    parser = Mock(spec=MaterialParser)
    service = MaterialService(InMemoryMaterialRepository(), parser)
    limit = 20 * 1024 * 1024
    service.create(filename='large.txt', content=b'x' * limit, idempotency_key='limit', defer_parse=True)
    with pytest.raises(materials.MaterialTooLarge):
        service.create(filename='large.txt', content=b'x' * (limit + 1), idempotency_key='over')
    parser.parse.assert_not_called()
    with pytest.raises(UnsupportedMaterial):
        service.create(filename='bad.docx', content=b'body', idempotency_key='unsupported', defer_parse=True)


@pytest.mark.parametrize('content', [b'', b'  \n\t', b'# Only a heading'])
def test_empty_material_has_stable_error_code(content):
    with pytest.raises(MaterialParseError) as exc:
        MaterialParser().parse(content, filename='empty.txt')
    assert exc.value.code == 'EMPTY_MATERIAL'


def test_pdf_errors_have_safe_specific_codes():
    from pypdf import PdfWriter
    cases = [(1, False, 'SCANNED_PDF_UNSUPPORTED'), (1, True, 'ENCRYPTED_PDF'), (301, False, 'PDF_PAGE_LIMIT_EXCEEDED')]
    for count, encrypted, code in cases:
        writer = PdfWriter()
        for _ in range(count):
            writer.add_blank_page(width=72, height=72)
        if encrypted:
            writer.encrypt('secret')
        buffer = io.BytesIO()
        writer.write(buffer)
        with pytest.raises(MaterialParseError) as exc:
            MaterialParser().parse(buffer.getvalue(), filename='input.pdf')
        assert exc.value.code == code
    with pytest.raises(MaterialParseError) as exc:
        MaterialParser().parse(b'not a pdf', filename='broken.pdf')
    assert exc.value.code == 'MATERIAL_PARSE_FAILED'


def test_raw_schema_uses_mysql_longblob_and_cascades_version_delete(tmp_path):
    from sqlalchemy import delete, event
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable
    from knowpath_backend.learning.db import MaterialRawRow, MaterialVersionRow

    assert "LONGBLOB" in str(CreateTable(MaterialRawRow.__table__).compile(dialect=mysql.dialect()))
    engine = create_engine(f'sqlite:///{tmp_path / "cascade.db"}')
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    repository = SqlAlchemyMaterialRepository(engine)
    result = create(MaterialService(repository), defer_parse=True)
    with engine.begin() as connection:
        connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.id == result.version.id))
    assert repository.get_raw(result.version.id) is None


def test_raw_migration_matches_metadata_and_preserves_old_tables(tmp_path, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect

    backend = Path(__file__).resolve().parents[2]
    url = f'sqlite:///{(tmp_path / "migration.db").as_posix()}'
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    engine = create_engine(url)
    command.upgrade(config, "0012_correction_context")
    original = set(inspect(engine).get_table_names())
    command.upgrade(config, "0013_material_raw")
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(config, "0012_correction_context")
    assert set(inspect(engine).get_table_names()) == original
