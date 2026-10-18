"""InnoDB source publication versus graph reconciliation, in an isolated schema."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, get_ident
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select, text

from knowpath_backend.learning.materials.source_access import lock_source_dependencies
from knowpath_backend.learning.persistence.db import MaterialRow, MaterialVersionRow, OutboxEventRow
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository


@pytest.mark.skipif(not os.getenv('LEARNING_TEST_MYSQL_URL'), reason='requires explicit isolated MySQL URL')
def test_publication_fences_sources_before_outbox():
    engine = create_engine(os.environ['LEARNING_TEST_MYSQL_URL'])
    assert engine.dialect.name == 'mysql'
    material, version, queue = (str(uuid4()) for _ in range(3))
    at = datetime.now(timezone.utc)
    boundary, publisher = Event(), []
    with engine.begin() as conn:
        conn.execute(MaterialRow.__table__.insert(), dict(id=material, name='publication-lock', type='document',
            status='ready', current_version_id=version, version=1, size_bytes=1, created_at=at, updated_at=at))
        conn.execute(MaterialVersionRow.__table__.insert(), dict(id=version, material_id=material,
            filename='lock.txt', media_type='text/plain', content_hash=uuid4().hex * 2, size_bytes=1,
            status='ready', parser_version='1', created_at=at))
        conn.execute(OutboxEventRow.__table__.insert(), dict(id=queue, aggregate_type='lesson',
            aggregate_id=version, event_type='lesson.generate', payload={}, status='processing', created_at=at))

    def before(conn, cursor, statement, parameters, context, many):
        if publisher and get_ident() == publisher[0] and 'FROM materials ' in statement and 'FOR UPDATE' in statement:
            boundary.set()

    def publish():
        publisher.append(get_ident())
        repository = SqlAlchemyMaterialRepository(engine)
        with repository.transaction():
            with repository.unit_of_work.session() as session:
                session.execute(text('SET SESSION innodb_lock_wait_timeout=3'))
            lock_source_dependencies(repository, [{'material_id': material, 'material_version_id': version}])
            with repository.unit_of_work.session() as session:
                session.execute(select(OutboxEventRow).where(OutboxEventRow.id == queue).with_for_update()).all()

    event.listen(engine, 'before_cursor_execute', before)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as graph:
                graph.execute(text('SET SESSION innodb_lock_wait_timeout=3'))
                graph.execute(select(MaterialRow).where(MaterialRow.id == material).with_for_update()).all()
                graph.execute(select(MaterialVersionRow).where(MaterialVersionRow.id == version).with_for_update()).all()
                pending = pool.submit(publish)
                assert boundary.wait(5), 'publication did not reach source fence'
                graph.execute(select(OutboxEventRow).where(OutboxEventRow.id == queue).with_for_update()).all()
            pending.result(timeout=8)
    finally:
        event.remove(engine, 'before_cursor_execute', before)
        with engine.begin() as conn:
            conn.execute(delete(OutboxEventRow).where(OutboxEventRow.id == queue))
            conn.execute(delete(MaterialVersionRow).where(MaterialVersionRow.id == version))
            conn.execute(delete(MaterialRow).where(MaterialRow.id == material))
        engine.dispose()
