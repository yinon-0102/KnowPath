"""SQLite cycles and opt-in real MySQL DDL, JSON and rollback checks."""
import os
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def config():
    cfg = Config(str(Path(__file__).resolve().parents[2] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[2] / 'migrations'))
    return cfg


def test_new_migration_is_single_head():
    assert ScriptDirectory.from_config(config()).get_heads() == ['0022_agent_memory']


@pytest.mark.parametrize('backend', ['sqlite', 'mysql'])
def test_upgrade_write_rollback_unique_cascade_downgrade_upgrade(backend, tmp_path, monkeypatch):
    url = os.getenv('LEARNING_TEST_MYSQL_URL') if backend == 'mysql' else 'sqlite+pysqlite:///' + str(tmp_path / 'cycle.db')
    if not url: pytest.skip('requires explicit isolated MySQL URL')
    monkeypatch.setenv('DATABASE_URL', url)
    cfg = config()
    command.upgrade(cfg, '0021_space_scope_options')
    old_engine = sa.create_engine(url)
    if 'learning_memory_spaces' in sa.inspect(old_engine).get_table_names():
        command.downgrade(cfg, '0021_space_scope_options')
    assert 'learning_agent_calls' not in sa.inspect(old_engine).get_table_names()
    old_engine.dispose()
    command.upgrade(cfg, 'head')
    engine = sa.create_engine(url)
    if backend == 'sqlite':
        sa.event.listen(engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
    from knowpath_backend.learning.agent_memory.models import TABLES
    from knowpath_backend.learning.persistence.db import LearningSpaceRow, ConversationRow, LearningMessageRow
    sid, cid, mid = str(uuid4()), str(uuid4()), str(uuid4())
    now = datetime.now()
    with engine.begin() as conn:
        conn.execute(sa.insert(LearningSpaceRow).values(id=sid, name='memory-migration', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=now, updated_at=now))
        conn.execute(sa.insert(ConversationRow).values(id=cid, space_id=sid, created_at=now))
        scope = {'space_id': sid, 'scope_version': 1, 'request_scope_id': 'scope-1', 'bindings': [], 'material_ids': []}
        conn.execute(sa.insert(LearningMessageRow).values(id=mid, space_id=sid, conversation_id=cid, run_id=str(uuid4()), status='completed', message='Q', sequence=1, snapshot={'request_scope': scope}, response={'text': 'A'}, created_at=now))
    from knowpath_backend.learning.agent_memory.repository import SqlAlchemyMemoryRepository
    from knowpath_backend.learning.persistence.unit_of_work import SqlAlchemyUnitOfWork
    repo = SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine), sid)
    with repo.transaction(): repo.ensure_space(); repo.project_completed(mid)
    assert len(repo.items_for_turn(mid)) == 2
    from knowpath_backend.memory.playbook.card import PlaybookCard
    from knowpath_backend.memory.contracts import MemoryCheckpoint
    card_id = str(uuid4())
    unsent, sent = str(uuid4()), str(uuid4())
    run_id = repo._business_message(mid)['run_id']
    with repo.transaction():
        repo.put_card(PlaybookCard(id=card_id, content='JSON 中文', type='preference', source='user_explicit'))
        repo.save_dependency_set('migration-set', mid, [{'message_id': mid}])
        repo.save_checkpoint(MemoryCheckpoint(space_id=sid, conversation_id=cid, state={'dependency_set_ids': ['migration-set']}))
        repo.put_commit('turn:' + mid, {'message_id': mid}, {'message_id': mid, 'status': 'completed'})
        repo.reserve_call(unsent, mid, run_id, 0, 'agent', {'max_tokens': 100})
        repo.transition_call(unsent, 'failed')
        repo.reserve_call(sent, mid, run_id, 0, 'agent', {'max_cost': 0.5})
        repo.transition_call(sent, 'sent')
        repo.transition_call(sent, 'failed', {'cost': 0.125})
    restored = SqlAlchemyMemoryRepository(SqlAlchemyUnitOfWork(engine), sid)
    assert restored.get_card(card_id).content == 'JSON 中文'
    assert restored.get_dependency_set('migration-set') == [{'message_id': mid}]
    assert restored.get_checkpoint(cid, 0).state == {'dependency_set_ids': ['migration-set']}
    assert restored.get_commit('turn:' + mid)['receipt'] == {'message_id': mid, 'status': 'completed'}
    assert restored._one('calls', call_id=unsent)['possibly_sent'] is False
    assert restored._one('calls', call_id=sent)['possibly_sent'] is True
    assert restored._one('calls', call_id=sent)['usage'] == {'cost': 0.125}
    assert engine.dialect.name == backend if backend == 'mysql' else engine.dialect.name == 'sqlite'
    item_table = TABLES['items']
    row = None
    with engine.connect() as conn:
        row = dict(conn.execute(sa.select(item_table).where(item_table.c.space_id == sid)).mappings().first())
        assert row['payload']['content'] in ('Q', 'A')
        indexes = sa.inspect(conn).get_indexes(item_table.name)
        assert any(i['column_names'] == ['space_id', 'source_version', 'embedding_model', 'embedding_dimension'] for i in indexes)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn: conn.execute(sa.insert(item_table).values(**dict(row, item_id=str(uuid4()))))
    with pytest.raises(RuntimeError):
        with repo.transaction(): repo.forget_item(row['item_id']); raise RuntimeError('rollback')
    assert repo.get_item(row['item_id'])
    # A valid existing message from another space must still fail the composite FK.
    other = str(uuid4())
    with engine.begin() as conn:
        conn.execute(sa.insert(LearningSpaceRow).values(id=other, name='other', status='active', bindings=[], topic_ids=[], excluded_topic_ids=[], created_at=now, updated_at=now))
        conn.execute(sa.insert(TABLES['spaces']).values(space_id=other))
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn: conn.execute(sa.insert(item_table).values(**dict(row, space_id=other, item_id=str(uuid4()))))
    sentinel = str(uuid4())
    with engine.begin() as conn:
        conn.execute(sa.delete(LearningMessageRow).where(LearningMessageRow.id == mid))
        assert conn.scalar(sa.select(sa.func.count()).select_from(item_table).where(item_table.c.space_id == sid)) == 0
        conn.execute(sa.delete(TABLES['spaces']).where(TABLES['spaces'].c.space_id.in_([sid, other])))
        conn.execute(sa.insert(LearningMessageRow).values(id=sentinel, space_id=sid, conversation_id=cid, run_id=str(uuid4()), status='completed', message='survives downgrade', sequence=2, snapshot={}, response={'text': 'retained'}, created_at=now))
    command.downgrade(cfg, '0021_space_scope_options')
    assert not any(n.startswith('learning_memory_') for n in sa.inspect(engine).get_table_names())
    assert 'learning_agent_calls' not in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.scalar(sa.select(LearningMessageRow.message).where(LearningMessageRow.id == sentinel)) == 'survives downgrade'
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.insert(LearningMessageRow).values(id=str(uuid4()), space_id=sid, conversation_id=cid, run_id=str(uuid4()), status='completed', message='duplicate sequence', sequence=2, snapshot={}, response={}, created_at=now))
    command.upgrade(cfg, 'head')
    assert len([n for n in sa.inspect(engine).get_table_names() if n.startswith('learning_memory_') or n == 'learning_agent_calls']) == 8
    with engine.begin() as conn:
        conn.execute(sa.delete(LearningMessageRow).where(LearningMessageRow.id == sentinel))
        conn.execute(sa.delete(ConversationRow).where(ConversationRow.id == cid))
        conn.execute(sa.delete(LearningSpaceRow).where(LearningSpaceRow.id.in_([sid, other])))
    engine.dispose()
