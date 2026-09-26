"""幂等迁移 SQL 原文件；默认仅预览，显式参数才清理 SQL 备份。"""
from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

from sqlalchemy import func, select, or_
from ..persistence.db import MaterialRawRow, MaterialRow, MaterialVersionRow, create_db_engine
from ..persistence.material_repository import SqlAlchemyMaterialRepository
from .raw_storage import RawStorageError, raw_object_key


def migrate_inline(repository, version_id, *, prune=False):
    with repository.unit_of_work.transaction():
        with repository.unit_of_work.session() as session:
            # 与删除操作保持同一锁定顺序，迁移提交前不能丢失对象清理引用。
            material_id = select(MaterialVersionRow.material_id).where(MaterialVersionRow.id == version_id).scalar_subquery()
            material = session.scalar(select(MaterialRow).where(MaterialRow.id == material_id).with_for_update())
            if material is None:
                return 'skipped'
            version = session.scalar(select(MaterialVersionRow).where(MaterialVersionRow.id == version_id).with_for_update())
            row = session.scalar(select(MaterialRawRow).where(MaterialRawRow.version_id == version_id).with_for_update())
            if version is None or row is None:
                return 'skipped'
            if row.storage_backend == 'minio':
                if not prune or row.content is None:
                    return 'skipped'
                repository._read_raw(row, version)
                row.content = None
                return 'pruned'
            store = repository.raw_store
            if store is None or row.storage_backend != 'sql' or row.content is None:
                raise RawStorageError('需要有效的 SQL 原文件与 MinIO 配置')
            content = bytes(row.content)
            if len(content) != version.size_bytes or sha256(content).hexdigest() != version.content_hash:
                raise RawStorageError('旧原文件哈希校验失败')
            key = raw_object_key(version_id)
            repository._compensate_on_rollback(session, version_id, key)
            etag = store.put(key, content, content_type=version.media_type)
            if store.get(key) != content:
                raise RawStorageError('迁移对象回读校验失败')
            row.storage_backend, row.bucket, row.object_key, row.etag = store.backend, store.bucket, key, etag
            if prune:
                row.content = None
            return 'migrated'


def migrate_all(repository, *, apply=False, prune=False, batch_size=100):
    if batch_size < 1 or batch_size > 1000:
        raise ValueError('批次大小必须在 1 到 1000 之间')
    condition = MaterialRawRow.storage_backend == 'sql'
    if prune:
        condition = or_(condition, MaterialRawRow.content.is_not(None))
    with repository.unit_of_work.session() as session:
        pending = session.scalar(select(func.count()).select_from(MaterialRawRow).where(condition))
    result = dict(pending=pending, migrated=0, pruned=0, skipped=0, dry_run=not apply)
    if not apply:
        return result
    cursor = ''
    while True:
        with repository.unit_of_work.session() as session:
            ids = list(session.scalars(select(MaterialRawRow.version_id).where(condition,
                MaterialRawRow.version_id > cursor).order_by(MaterialRawRow.version_id).limit(batch_size)))
        if not ids:
            return result
        for version_id in ids:
            result[migrate_inline(repository, version_id, prune=prune)] += 1
        cursor = ids[-1]


def main(argv=None):
    parser = argparse.ArgumentParser(description='将旧 SQL 原文件迁移至 MinIO，默认仅预览')
    parser.add_argument('--apply', action='store_true', help='执行迁移，每个版本独立提交')
    parser.add_argument('--prune-sql', action='store_true', help='回读校验成功后清除 SQL 原文件备份')
    parser.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args(argv)
    if args.prune_sql and not args.apply:
        parser.error('--prune-sql 必须与 --apply 同时使用')
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / '.env')
    engine = repository = None
    try:
        engine = create_db_engine()
        repository = SqlAlchemyMaterialRepository.from_env(engine)
        if args.apply and repository.raw_store is None:
            raise ValueError('请配置 LEARNING_RAW_STORAGE=minio')
        result = migrate_all(repository, apply=args.apply, prune=args.prune_sql, batch_size=args.batch_size)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print('RAW_MIGRATION_FAILED：已提交的版本可重放，请检查配置和存储连接后重试', file=sys.stderr)
        return 1
    finally:
        if repository is not None:
            repository.close()
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
