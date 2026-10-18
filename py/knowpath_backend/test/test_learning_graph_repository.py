"""Large graph history reads must use the existing sequence index, without filesort."""
from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects import mysql

from knowpath_backend.learning.persistence.db import GraphRevisionRow, OutboxEventRow
from knowpath_backend.learning.persistence.graph_repository import SqlAlchemyGraphRepository
from knowpath_backend.learning.persistence.learning_repository import SqlAlchemyLearningRepository
from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture
def graph_repository():
    engine = create_engine('sqlite+pysqlite:///:memory:')
    GraphRevisionRow.__table__.create(engine)
    OutboxEventRow.__table__.create(engine)
    repository = SqlAlchemyGraphRepository(SqlAlchemyUnitOfWork(engine))
    large = {'nodes': [{'text': 'large graph snapshot ' * 16000}], 'edges': []}
    for identifier, material, sequence, day, status in [
        ('z', 'material', 1, 2, 'published'),
        ('b', 'material', 2, 0, 'superseded'),
        ('a', 'material', 3, 0, 'published'),
        ('foreign', 'other-material', 1, 0, 'published'),
    ]:
        repository.put_record('graph_revisions', {
            'id': identifier, 'material_id': material, 'material_version_id': 'version',
            'sequence': sequence, 'base_graph_version': 0, 'graph_version': sequence,
            'status': status, 'run_id': 'run', 'snapshot': large,
            'snapshot_hash': identifier, 'diff': {'added': large},
            'created_at': (datetime(2026, 1, 1) + timedelta(days=day)).isoformat(),
        })
    try:
        yield repository
    finally:
        engine.dispose()


@pytest.mark.parametrize('transaction,lock', [(False, True), (True, True), (True, False)])
def test_large_history_uses_sequence_index_and_preserves_api_order_and_locking(graph_repository, transaction, lock):
    repository = graph_repository
    engine = repository.unit_of_work.engine
    statements = []

    def capture(connection, statement, multiparams, params, options):
        if statement.is_select:
            statements.append((statement, dict(options)))

    event.listen(engine, 'before_execute', capture)
    try:
        with repository.transaction() if transaction else nullcontext():
            rows = repository.records('graph_revisions', material_id='material', lock=lock)
    finally:
        event.remove(engine, 'before_execute', capture)

    assert [row['id'] for row in rows] == ['a', 'b', 'z'], 'public order remains (created_at, id), not SQL sequence'
    assert all(len(row['snapshot']['nodes'][0]['text']) > 262144 for row in rows)
    assert [row['snapshot_hash'] for row in rows] == ['a', 'b', 'z']
    statement, options = statements[0]
    sql = str(statement.compile(dialect=mysql.dialect()))
    assert 'ORDER BY graph_revisions.material_id, graph_revisions.sequence' in sql
    assert ('FOR UPDATE' in sql) is (transaction and lock)
    assert bool(options.get('populate_existing')) is (transaction and lock)
    with engine.connect() as connection:
        compiled = statement.compile(dialect=engine.dialect, compile_kwargs={'literal_binds': True})
        plan = connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + str(compiled)).all()
    assert not any('TEMP B-TREE' in row[-1] for row in plan), plan
    assert any('INDEX' in row[-1] for row in plan), plan


def test_history_keeps_additional_filters_empty_results_and_defensive_copies(graph_repository):
    repository = graph_repository
    rows = repository.records('graph_revisions', material_id='material', status='published', lock=False)
    assert [row['id'] for row in rows] == ['a', 'z']
    rows[0]['snapshot']['nodes'].clear()
    assert repository.records('graph_revisions', material_id='material', sequence=3)[0]['snapshot']['nodes']
    assert repository.records('graph_revisions', material_id='missing') == []
    assert {row['id'] for row in repository.records('graph_revisions')} == {'a', 'b', 'z', 'foreign'}


def test_transactional_history_refreshes_existing_identity_map_rows(graph_repository):
    repository = graph_repository
    with repository.transaction():
        with repository.unit_of_work.session() as session:
            cached = session.scalar(select(GraphRevisionRow).where(GraphRevisionRow.id == 'a'))
            session.execute(update(GraphRevisionRow).where(GraphRevisionRow.id == 'a')
                            .values(status='superseded').execution_options(synchronize_session=False))
            assert cached.status == 'published'
            rows = repository.records('graph_revisions', material_id='material')
            assert rows[0]['status'] == 'superseded'
            assert cached.status == 'superseded'


def test_other_tables_and_generic_repository_keep_id_lock_order(graph_repository):
    repository = graph_repository
    engine = repository.unit_of_work.engine
    statements = []

    def capture(connection, statement, multiparams, params, options):
        if statement.is_select:
            statements.append(str(statement.compile(dialect=mysql.dialect())))

    event.listen(engine, 'before_execute', capture)
    try:
        with repository.transaction():
            assert repository.records('outbox', aggregate_id='missing') == []
            SqlAlchemyLearningRepository(repository.unit_of_work).records('graph_revisions', material_id='material')
    finally:
        event.remove(engine, 'before_execute', capture)
    assert 'ORDER BY outbox_events.id FOR UPDATE' in statements[0]
    assert 'ORDER BY graph_revisions.id FOR UPDATE' in statements[1]
