"""原始资料对象存储端口；业务路由不依赖 MinIO SDK。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from typing import Protocol


class RawStorageError(RuntimeError):
    """对外使用固定错误信息，不泄露连接地址或凭据。"""


@dataclass(frozen=True)
class ObjectStorageSettings:
    endpoint: str = '127.0.0.1:9000'
    access_key: str = field(default='', repr=False)
    secret_key: str = field(default='', repr=False)
    bucket: str = 'knowpath-materials'
    secure: bool = False

    @classmethod
    def from_env(cls):
        secure = os.getenv('MINIO_SECURE', 'false').lower()
        if secure not in {'true', 'false'}:
            raise ValueError('MINIO_SECURE 必须为 true 或 false')
        return cls(endpoint=os.getenv('MINIO_ENDPOINT', cls.endpoint),
                   access_key=os.getenv('MINIO_ACCESS_KEY', ''),
                   secret_key=os.getenv('MINIO_SECRET_KEY', ''),
                   bucket=os.getenv('MINIO_BUCKET', cls.bucket), secure=secure == 'true')


def raw_object_key(version_id: str, filename: str | None = None) -> str:
    # 文件名只保留在 SQL 元数据中，不能影响对象路径。
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', version_id):
        raise ValueError('无效的资料版本标识')
    return f'materials/{version_id}/raw'


class RawMaterialStore(Protocol):
    backend: str
    bucket: str

    def put(self, key: str, content: bytes, *, content_type: str) -> str: ...
    def get(self, key: str) -> bytes | None: ...
    def delete(self, key: str) -> None: ...


class InMemoryRawMaterialStore:
    """测试替身：以相同端口模拟外部对象存储。"""
    backend = 'minio'

    def __init__(self, bucket='knowpath-materials'):
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def put(self, key, content, *, content_type='application/octet-stream'):
        self.objects[key] = bytes(content)
        return sha256(content).hexdigest()

    def get(self, key):
        return self.objects.get(key)

    def delete(self, key):
        self.objects.pop(key, None)


class MinioRawMaterialStore:
    backend = 'minio'

    def __init__(self, settings: ObjectStorageSettings, *, client=None):
        from minio import Minio
        from urllib3 import PoolManager, Timeout
        from urllib3.util.retry import Retry
        if not settings.access_key or not settings.secret_key:
            raise ValueError('启用 MinIO 需要 MINIO_ACCESS_KEY 和 MINIO_SECRET_KEY')
        self.bucket = settings.bucket
        self._pool = None
        if client is None:
            self._pool = PoolManager(timeout=Timeout(connect=5, read=30),
                                     retries=Retry(total=2, backoff_factor=0.2))
            client = Minio(settings.endpoint, access_key=settings.access_key,
                           secret_key=settings.secret_key, secure=settings.secure,
                           http_client=self._pool)
        self._client = client

    def put(self, key, content, *, content_type='application/octet-stream'):
        try:
            result = self._client.put_object(self.bucket, key, BytesIO(content),
                                            len(content), content_type=content_type)
            return result.etag
        except Exception as exc:
            raise RawStorageError('原始资料对象写入失败') from exc

    def get(self, key):
        from minio.error import S3Error
        response = None
        try:
            response = self._client.get_object(self.bucket, key)
            return response.read()
        except S3Error as exc:
            if exc.code == 'NoSuchKey':
                return None
            raise RawStorageError('原始资料对象读取失败') from exc
        except Exception as exc:
            raise RawStorageError('原始资料对象读取失败') from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def delete(self, key):
        from minio.error import S3Error
        try:
            self._client.remove_object(self.bucket, key)
            # 启用版本控制的桶还须删除历史版本及删除标记，不能只隐藏当前对象。
            for item in self._client.list_objects(self.bucket, prefix=key, recursive=True, include_version=True):
                if item.object_name == key:
                    self._client.remove_object(self.bucket, key, version_id=item.version_id)
            if any(item.object_name == key for item in self._client.list_objects(
                    self.bucket, prefix=key, recursive=True, include_version=True)):
                raise RawStorageError('原始资料历史版本删除未确认')
            try:
                self._client.stat_object(self.bucket, key)
            except S3Error as exc:
                if exc.code == 'NoSuchKey':
                    return
                raise
            raise RawStorageError('原始资料对象删除未确认')
        except RawStorageError:
            raise
        except Exception as exc:
            raise RawStorageError('原始资料对象删除失败') from exc

    def close(self):
        if self._pool is not None:
            self._pool.clear()


def configured_raw_store() -> RawMaterialStore | None:
    backend = os.getenv('LEARNING_RAW_STORAGE', 'sql').lower()
    if backend == 'sql':
        return None
    if backend == 'minio':
        return MinioRawMaterialStore(ObjectStorageSettings.from_env())
    raise ValueError('LEARNING_RAW_STORAGE 只能是 sql 或 minio')
