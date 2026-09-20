"""Opt-in real MySQL checks using a freshly created, uniquely named test DB.

LEARNING_TEST_MYSQL_ADMIN_URL grants CREATE/DROP only for this opt-in run. No
existing database is migrated or cleaned. Credentials are never logged here.
"""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from knowpath_backend.learning.persistence.db import Base
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository, RagIntegrityError
from knowpath_backend.test.test_rag_repository import seed, span


@pytest.fixture
def mysql_rag(monkeypatch):
    admin_url = os.getenv("LEARNING_TEST_MYSQL_ADMIN_URL")
    if not admin_url:
        pytest.skip("requires opt-in MySQL admin URL for isolated test DB")
    name = "knowpath_rag_test_" + uuid4().hex
    assert re.fullmatch(r"knowpath_rag_test_[0-9a-f]{32}", name)
    admin = sa.create_engine(admin_url, hide_parameters=True)
    assert admin.dialect.name == "mysql"
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
        created = True
        url = sa.engine.make_url(admin_url).set(database=name)
        backend = Path(__file__).resolve().parents[2]
        monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "migrations"))
        command.upgrade(config, "0013_material_raw")
        engine = sa.create_engine(url, hide_parameters=True)
        tables = sa.MetaData()
        tables.reflect(engine)
        now = datetime(2026, 9, 20)
        with engine.begin() as connection:
            connection.execute(tables.tables["materials"].insert(), dict(id="material", name="Notes", type="txt",
                status="ready", size_bytes=20, created_at=now, updated_at=now))
            connection.execute(tables.tables["material_versions"].insert(), dict(id="mv", material_id="material",
                filename="notes.txt", media_type="text/plain", content_hash="a" * 64, size_bytes=20,
                status="ready", graph_version=1, created_at=now))
            connection.execute(tables.tables["source_chunks"].insert(), dict(id="old", material_version_id="mv",
                text="original", section_path=[], content_hash="c" * 64))
            connection.execute(tables.tables["learning_spaces"].insert(), dict(id="space", name="Study", status="active",
                bindings=[], topic_ids=[], excluded_topic_ids=[], space_version=1, scope_version=1,
                profile_version=1, state_version=0, profile={}, created_at=now, updated_at=now))
        command.upgrade(config, "head")
        yield SqlRagRepository(engine), engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()


def test_mysql_real_upgrade_mapping_and_erasure(mysql_rag):
    store, engine = mysql_rag
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0014_rag_snapshots"
        assert connection.scalar(sa.text("SELECT text FROM source_chunks WHERE id='old'")) == "original"
    seed(store)
    store.put_scope_chunk_map(dict(scope_snapshot_id="scope", retrieval_version_id="rv", chunk_id="chunk"))
    assert len(store.list_scope_chunks("scope", "rv")) == 1
    SqlAlchemyMaterialRepository(engine).delete_material("material")
    assert store.get_retrieval_version("rv") is None
    assert store.get_scope_snapshot("scope") is None


def test_mysql_cleanup_sees_scope_committed_after_earlier_repeatable_read(mysql_rag):
    store, engine = mysql_rag
    materials = SqlAlchemyMaterialRepository(engine)
    scopes = Base.metadata.tables["rag_scope_snapshots"]
    with materials.transaction():
        with materials.unit_of_work.session() as session:
            assert session.execute(sa.select(scopes)).all() == []  # establish RR snapshot
        store.put_scope_snapshot(dict(scope_snapshot_id="late", space_id="space", scope_version=1,
            bindings=[dict(material_id="material", material_version_id="mv", graph_version=1)]), [span()])
        materials.delete_material("material")
    assert store.get_scope_snapshot("late") is None


def test_mysql_empty_scope_cannot_commit_after_bound_material_erasure(mysql_rag, monkeypatch):
    store, engine = mysql_rag
    prechecked, erased = Event(), Event()
    original = store._require
    def delayed(connection, table, key, **kwargs):
        row = original(connection, table, key, **kwargs)
        if table.name == "learning_spaces":
            prechecked.set()
            assert erased.wait(10), "erasure did not complete"
        return row
    monkeypatch.setattr(store, "_require", delayed)
    payload = dict(scope_snapshot_id="empty", space_id="space", scope_version=1,
        bindings=[dict(material_id="material", material_version_id="mv", graph_version=1)])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.put_scope_snapshot, payload, [])
        try:
            assert prechecked.wait(10), "writer did not establish its read snapshot"
            SqlAlchemyMaterialRepository(engine).delete_material("material")
        finally:
            erased.set()
        with pytest.raises(RagIntegrityError):
            future.result(timeout=10)
    assert store.get_scope_snapshot("empty") is None
