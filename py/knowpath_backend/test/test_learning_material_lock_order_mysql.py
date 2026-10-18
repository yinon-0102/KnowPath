"""Real InnoDB regression for reconciliation versus source delivery locks."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, get_ident
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select, text

from knowpath_backend.learning.persistence.db import MaterialRow, MaterialVersionRow, SourceChunkRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository


@pytest.mark.skipif(not os.getenv('LEARNING_TEST_MYSQL_URL'), reason='requires explicit isolated MySQL URL')
def test_version_batch_and_source_delivery_do_not_invert_parent_chunk_locks():
    engine = create_engine(os.environ['LEARNING_TEST_MYSQL_URL'])
    assert engine.dialect.name == 'mysql'
    identifier, version, chunk = (str(uuid4()) for _ in range(3))
    at = datetime.now(timezone.utc)
    boundary = Event()
    worker_id = []
    with engine.begin() as connection:
        connection.execute(MaterialRow.__table__.insert(), dict(id=identifier, name='lock-regression', type='document',
            status='ready', current_version_id=version, version=1, size_bytes=1, created_at=at, updated_at=at))
        connection.execute(MaterialVersionRow.__table__.insert(), dict(id=version, material_id=identifier,
            filename='lock.txt', media_type='text/plain', content_hash=uuid4().hex * 2, size_bytes=1,
            status='ready', parser_version='1', created_at=at))
        connection.execute(SourceChunkRow.__table__.insert(), dict(id=chunk, material_version_id=version,
            text='source', section_path=[], content_hash=uuid4().hex * 2))

    def before(conn, cursor, statement, parameters, context, many):
        if worker_id and get_ident() == worker_id[0] and 'FROM material_versions' in statement and 'FOR UPDATE' in statement:
            boundary.set()

    def after(conn, cursor, statement, parameters, context, many):
        # The old implementation reaches this boundary holding child locks.
        if worker_id and get_ident() == worker_id[0] and 'FROM source_chunks' in statement and 'FOR UPDATE' in statement:
            boundary.set()

    def reconcile():
        worker_id.append(get_ident())
        repository = SqlAlchemyMaterialRepository(engine)
        with repository.transaction():
            with repository.unit_of_work.session() as session:
                session.execute(text('SET SESSION innodb_lock_wait_timeout=3'))
            versions = repository.list_versions(identifier)
            assert [v.id for v in versions] == [version]
            assert repository.get_version(version).chunks[0].text == 'source'

    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as owner:
                owner.execute(text('SET SESSION innodb_lock_wait_timeout=3'))
                owner.execute(select(MaterialVersionRow).where(MaterialVersionRow.id == version).with_for_update()).all()
                pending = pool.submit(reconcile)
                assert boundary.wait(5), 'reconciliation did not reach lock boundary'
                owner.execute(select(SourceChunkRow).where(SourceChunkRow.id == chunk).with_for_update()).all()
            pending.result(timeout=8)
    finally:
        event.remove(engine, 'before_cursor_execute', before)
        event.remove(engine, 'after_cursor_execute', after)
        with engine.begin() as connection:
            connection.execute(delete(SourceChunkRow).where(SourceChunkRow.id == chunk))
            connection.execute(delete(MaterialVersionRow).where(MaterialVersionRow.id == version))
            connection.execute(delete(MaterialRow).where(MaterialRow.id == identifier))
        engine.dispose()
