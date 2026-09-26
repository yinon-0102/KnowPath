from __future__ import annotations

import pytest

from knowpath_backend.learning.materials.raw_storage import (
    InMemoryRawMaterialStore,
    MinioRawMaterialStore,
    ObjectStorageSettings,
    raw_object_key,
)


def test_object_key_is_stable_and_does_not_include_filename():
    assert raw_object_key("version-123", "notes.pdf") == "materials/version-123/raw"


def test_in_memory_store_round_trip_and_idempotent_delete():
    store = InMemoryRawMaterialStore()
    store.put("materials/v/raw", b"hello", content_type="text/plain")
    assert store.get("materials/v/raw") == b"hello"
    store.delete("materials/v/raw")
    store.delete("materials/v/raw")


def test_minio_store_uses_s3_compatible_client(monkeypatch):
    calls = []

    class FakeResponse:
        def read(self):
            return b"hello"

        def close(self):
            calls.append(("close",))

        def release_conn(self):
            calls.append(("release",))

    class FakeClient:
        def put_object(self, bucket, key, stream, length, content_type):
            calls.append(("put", bucket, key, stream.read(), length, content_type))
            return type("Result", (), {"etag": "etag-1"})()

        def get_object(self, bucket, key):
            calls.append(("get", bucket, key))
            return FakeResponse()

        def remove_object(self, bucket, key):
            calls.append(("delete", bucket, key))

        def stat_object(self, bucket, key):
            from minio.error import S3Error
            raise S3Error(response=None, code="NoSuchKey", message="missing", resource=key, request_id="request", host_id="host")

        def list_objects(self, bucket, **kwargs):
            return []

    store = MinioRawMaterialStore(ObjectStorageSettings(endpoint="minio:9000", access_key="a", secret_key="b"), client=FakeClient())
    assert store.put("k", b"hello", content_type="text/plain") == "etag-1"
    assert store.get("k") == b"hello"
    store.delete("k")
    assert calls == [
        ("put", "knowpath-materials", "k", b"hello", 5, "text/plain"),
        ("get", "knowpath-materials", "k"),
        ("close",),
        ("release",),
        ("delete", "knowpath-materials", "k"),
    ]


@pytest.mark.parametrize('value', ['yes', 'FALSE ', 'unexpected'])
def test_invalid_secure_flag_is_rejected(monkeypatch, value):
    monkeypatch.setenv('MINIO_SECURE', value)
    with pytest.raises(ValueError):
        ObjectStorageSettings.from_env()


def test_settings_do_not_expose_credentials(monkeypatch):
    monkeypatch.setenv('MINIO_ACCESS_KEY', 'test-access')
    monkeypatch.setenv('MINIO_SECRET_KEY', 'test-secret')
    monkeypatch.setenv('MINIO_SECURE', 'true')
    settings = ObjectStorageSettings.from_env()
    assert settings.secure is True
    assert 'test-secret' not in repr(settings)
    assert 'test-access' not in repr(settings)


@pytest.mark.parametrize('version', ['../escape', '', 'a/b', 'a\\b'])
def test_object_key_rejects_paths(version):
    with pytest.raises(ValueError):
        raw_object_key(version)


@pytest.mark.parametrize('failure', ['still-present', 'AccessDenied'])
def test_delete_does_not_report_unconfirmed_erasure(failure):
    from minio.error import S3Error
    from knowpath_backend.learning.materials.raw_storage import RawStorageError
    class Client:
        def remove_object(self, *args, **kwargs):
            pass
        def list_objects(self, *args, **kwargs):
            return []
        def stat_object(self, *args):
            if failure == 'AccessDenied':
                raise S3Error(response=None, code=failure, message='private', resource='', request_id='', host_id='')
            return object()
    store = MinioRawMaterialStore(ObjectStorageSettings(access_key='a', secret_key='b'), client=Client())
    with pytest.raises(RawStorageError):
        store.delete('k')
