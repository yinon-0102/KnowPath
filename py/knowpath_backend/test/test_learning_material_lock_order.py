"""Version lists must take the same parent-first locks as a version lookup."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects import mysql

from knowpath_backend.learning.persistence.db import init_db, MaterialRow, MaterialVersionRow, SourceChunkRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'versions.db'}")
    init_db(engine)
    at = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(MaterialRow.__table__.insert(), dict(id='material', name='notes', type='document', status='ready',
            current_version_id='b', version=2, size_bytes=1, created_at=at, updated_at=at))
        for identifier in ['b', 'a']:
            conn.execute(MaterialVersionRow.__table__.insert(), dict(id=identifier, material_id='material', filename='notes.txt',
                media_type='text/plain', content_hash=identifier * 64, size_bytes=1, status='ready', parser_version='1', created_at=at))
            conn.execute(SourceChunkRow.__table__.insert(), dict(id='chunk-'+identifier, material_version_id=identifier,
                text=identifier, section_path=[], content_hash=identifier * 64))
    yield SqlAlchemyMaterialRepository(engine)
    engine.dispose()


@pytest.mark.parametrize('transactional', [False, True])
def test_list_versions_locks_ordered_parents_before_any_chunk(repository, transactional):
    from contextlib import nullcontext
    statements = []
    def capture(conn, statement, multiparams, params, options):
        if statement.is_select:
            statements.append((str(statement.compile(dialect=mysql.dialect())), options))
    engine = repository.unit_of_work.engine
    event.listen(engine, 'before_execute', capture)
    try:
        with repository.transaction() if transactional else nullcontext():
            versions = repository.list_versions('material')
    finally:
        event.remove(engine, 'before_execute', capture)
    sql, options = statements[0]
    assert 'ORDER BY material_versions.id' in sql
    assert ('FOR UPDATE' in sql) == transactional
    assert bool(options.get('populate_existing')) == transactional
    assert all(('FOR UPDATE' in stmt) == transactional for stmt, _ in statements[1:])
    assert [v.id for v in versions] == ['a', 'b']
    assert [v.chunks[0].text for v in versions] == ['a', 'b']


def test_list_versions_refreshes_previously_cached_parent(repository):
    with repository.transaction():
        with repository.unit_of_work.session() as session:
            cached = session.scalar(select(MaterialVersionRow).where(MaterialVersionRow.id == 'a'))
            session.execute(update(MaterialVersionRow).where(MaterialVersionRow.id == 'a').values(status='failed')
                .execution_options(synchronize_session=False))
            assert cached.status == 'ready'
            versions = repository.list_versions('material')
            assert next(v for v in versions if v.id == 'a').status == 'failed'
            assert cached.status == 'failed'
