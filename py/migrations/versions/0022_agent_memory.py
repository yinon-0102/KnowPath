"""Additive memory schema. Indexed identities fit MySQL 5.7 utf8mb4 limits."""
import sqlalchemy as sa



def memory_tables(metadata):
    tables = {}
    def table(key, *columns):
        name = 'learning_agent_calls' if key == 'calls' else 'learning_memory_' + key
        tables[key] = sa.Table(name, metadata, *columns)
        return tables[key]
    def text(name, length=64, **kw): return sa.Column(name, sa.String(length), **kw)
    def integer(name, default=0): return sa.Column(name, sa.Integer, nullable=False, server_default=str(default))
    def space(): return text('space_id', 36, nullable=False)
    def space_fk(): return sa.ForeignKeyConstraint(['space_id'], ['learning_memory_spaces.space_id'], ondelete='CASCADE')
    table('spaces', sa.Column('space_id', sa.String(36), sa.ForeignKey('learning_spaces.id', ondelete='CASCADE'), primary_key=True),
          integer('revision'), integer('deletion_generation'), integer('receive_sequence'),
          text('migration_status', 32, nullable=False, server_default='pending'),
          text('lease_token'), text('lease_owner', 128), sa.Column('lease_expires', sa.DateTime))
    items = table('items', text('item_id', 36, primary_key=True), space(), text('message_id', 36, nullable=False),
          text('role', 16, nullable=False), sa.Column('content', sa.Text, nullable=False), text('checksum', nullable=False),
          text('lifecycle', 32, nullable=False, server_default='active'), integer('access_count'),
          text('scope_hash', nullable=False), text('source_version', nullable=False),
          sa.Column('payload', sa.JSON, nullable=False), sa.Column('vector', sa.JSON),
          text('embedding_model', 128), sa.Column('embedding_dimension', sa.Integer),
          text('embedding_type', 16, nullable=False, server_default='document'), integer('index_generation'),
          space_fk(), sa.ForeignKeyConstraint(['space_id', 'message_id'], ['learning_messages.space_id', 'learning_messages.id'], ondelete='CASCADE'),
          sa.UniqueConstraint('message_id', 'role', name='uq_memory_message_role'))
    sa.Index('ix_memory_items_scope', items.c.space_id, items.c.scope_hash, items.c.lifecycle)
    sa.Index('ix_memory_items_embedding', items.c.space_id, items.c.source_version, items.c.embedding_model, items.c.embedding_dimension)
    cards = table('cards', text('card_id', 36, primary_key=True), space(), text('source_hash', nullable=False),
          text('scope_hash', nullable=False), text('lifecycle', 32, nullable=False), sa.Column('pinned', sa.Boolean, nullable=False),
          sa.Column('payload', sa.JSON, nullable=False), space_fk(), sa.UniqueConstraint('space_id', 'source_hash', name='uq_memory_card_source'))
    sa.Index('ix_memory_cards_scope', cards.c.space_id, cards.c.scope_hash, cards.c.lifecycle)
    table('sessions', text('session_id', 36, primary_key=True), space(), text('conversation_id', 36, nullable=False),
          integer('context_epoch'), text('fingerprint', 128, nullable=False), integer('revision'), integer('version'),
          sa.Column('checkpoint', sa.JSON, nullable=False), space_fk(),
          sa.ForeignKeyConstraint(['space_id', 'conversation_id'], ['learning_conversations.space_id', 'learning_conversations.id'], ondelete='CASCADE'),
          sa.UniqueConstraint('space_id', 'conversation_id', 'context_epoch', name='uq_memory_session_epoch'))
    deps = table('dependencies', text('dependency_id', 36, primary_key=True), space(), text('set_id', 128, nullable=False),
          text('derived_id', 128, nullable=False), integer('ordinal'), text('record_hash', nullable=False),
          sa.Column('record', sa.JSON, nullable=False),
          text('message_id', 128), text('item_id', 128), text('material_version_id', 128), text('chunk_id', 128),
          text('retrieval_version_id', 128), text('artifact_hash', 64), text('block', 128),
          text('owner_message_id', 36), space_fk(),
          sa.ForeignKeyConstraint(['space_id', 'owner_message_id'], ['learning_messages.space_id', 'learning_messages.id'], ondelete='CASCADE'),
          sa.UniqueConstraint('space_id', 'set_id', 'ordinal', name='uq_memory_dependency_ordinal'))
    sa.Index('ix_memory_dependency_reverse', deps.c.space_id, deps.c.record_hash)
    sa.Index('ix_memory_dependency_derived', deps.c.space_id, deps.c.derived_id)
    for key in ('message_id', 'item_id', 'material_version_id', 'chunk_id', 'retrieval_version_id', 'artifact_hash', 'block'):
        sa.Index('ix_memory_dependency_' + key, deps.c.space_id, deps.c[key])
    table('commits', text('commit_id', 128, primary_key=True), space(), text('fingerprint', nullable=False),
          integer('revision'), sa.Column('receipt', sa.JSON, nullable=False), space_fk())
    table('tombstones', text('tombstone_id', 36, primary_key=True), space(), text('target_hash', nullable=False),
          text('kind', 32, nullable=False), integer('deletion_generation'), space_fk(),
          sa.UniqueConstraint('space_id', 'target_hash', 'kind', name='uq_memory_tombstone_target'))
    table('calls', text('call_id', 36, primary_key=True), space(), text('message_id', 36, nullable=False),
          text('run_id', 36, nullable=False), integer('attempt'), text('runtime', 64, nullable=False),
          text('status', 16, nullable=False), sa.Column('budget', sa.JSON, nullable=False), sa.Column('usage', sa.JSON, nullable=False),
          sa.Column('possibly_sent', sa.Boolean, nullable=False, server_default=sa.false()),
          space_fk(), sa.ForeignKeyConstraint(['space_id', 'message_id'], ['learning_messages.space_id', 'learning_messages.id'], ondelete='CASCADE'))
    return tables




from alembic import op

revision = '0022_agent_memory'
down_revision = '0021_space_scope_options'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('uq_conversation_space_id', 'learning_conversations', ['space_id', 'id'], unique=True)
    op.create_index('uq_message_space_id', 'learning_messages', ['space_id', 'id'], unique=True)
    metadata = sa.MetaData()
    # Frozen parent stubs support offline SQL generation without reflection.
    sa.Table('learning_spaces', metadata, sa.Column('id', sa.String(36), primary_key=True))
    for name in ('learning_messages', 'learning_conversations'):
        sa.Table(name, metadata, sa.Column('id', sa.String(36), primary_key=True), sa.Column('space_id', sa.String(36)))
    for table in memory_tables(metadata).values():
        table.create(op.get_bind())


def downgrade():
    for name in ('learning_agent_calls', 'learning_memory_tombstones', 'learning_memory_commits', 'learning_memory_dependencies', 'learning_memory_sessions', 'learning_memory_cards', 'learning_memory_items', 'learning_memory_spaces'):
        op.drop_table(name)
    op.drop_index('uq_message_space_id', table_name='learning_messages')
    op.drop_index('uq_conversation_space_id', table_name='learning_conversations')
