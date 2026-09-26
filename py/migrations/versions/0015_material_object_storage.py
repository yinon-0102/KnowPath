"""保存原始资料对象引用，同时保留旧 SQL 文件内容的兼容读取。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGBLOB

revision = '0015_material_object_storage'
down_revision = '0014_rag_snapshots'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('material_raw_files') as batch:
        batch.alter_column('content', existing_type=sa.LargeBinary().with_variant(LONGBLOB(), 'mysql'), nullable=True)
        batch.add_column(sa.Column('storage_backend', sa.String(16), nullable=False, server_default='sql'))
        batch.add_column(sa.Column('bucket', sa.String(63), nullable=True))
        batch.add_column(sa.Column('object_key', sa.String(512), nullable=True))
        batch.add_column(sa.Column('etag', sa.String(128), nullable=True))


def downgrade():
    # 对象引用不能随降级悄悄丢失；含 MinIO 文件时应恢复完整备份。
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM material_raw_files WHERE storage_backend <> 'sql' OR content IS NULL")):
        raise RuntimeError('存在对象存储资料，拒绝降级；请先备份并恢复为纯 SQL 存储')
    with op.batch_alter_table('material_raw_files') as batch:
        for column in ('etag', 'object_key', 'bucket', 'storage_backend'):
            batch.drop_column(column)
        batch.alter_column('content', existing_type=sa.LargeBinary().with_variant(LONGBLOB(), 'mysql'), nullable=False)
