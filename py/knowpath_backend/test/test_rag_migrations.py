"""RAG migration is additive, frozen, and compiles for production MySQL."""

from datetime import datetime
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from knowpath_backend.learning.persistence.db import Base


TABLES = {"rag_retrieval_versions", "rag_chunks", "rag_nodes", "rag_chunk_spans", "rag_scope_snapshots", "rag_scope_spans", "rag_scope_chunk_map", "rag_manifests", "rag_publications"}


def config_for(url, monkeypatch, output=None):
    backend = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(backend / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def test_additive_upgrade_preserves_legacy_sources_and_matches_metadata(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{(tmp_path / 'rag.db').as_posix()}"
    config = config_for(url, monkeypatch)
    command.upgrade(config, "0013_material_raw")
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    meta.reflect(engine)
    with engine.begin() as connection:
        now = datetime(2026, 9, 20)
        connection.execute(meta.tables["materials"].insert(), dict(id="m", name="notes", type="txt", status="ready", size_bytes=5, created_at=now, updated_at=now))
        connection.execute(meta.tables["material_versions"].insert(), dict(id="v", material_id="m", filename="notes.txt", media_type="text/plain", content_hash="a" * 64, size_bytes=5, status="ready", graph_version=0, created_at=now))
        connection.execute(meta.tables["source_chunks"].insert(), dict(id="legacy-c", material_version_id="v", text="hello", section_path=[], content_hash="a" * 64))
        connection.execute(meta.tables["material_raw_files"].insert(), dict(version_id="v", content=b"hello"))
    command.upgrade(config, "head")
    assert TABLES <= set(sa.inspect(engine).get_table_names()), "RAG migration tables are missing"
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT id, text FROM source_chunks")).all() == [("legacy-c", "hello")]
        assert connection.scalar(sa.text("SELECT content FROM material_raw_files")) == b"hello"
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
    command.downgrade(config, "0013_material_raw")
    assert not TABLES.intersection(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT text FROM source_chunks")) == "hello"
    engine.dispose()


def test_mysql_offline_upgrade_is_additive_and_frozen(monkeypatch):
    output = StringIO()
    config = config_for("mysql+pymysql://unused:unused@127.0.0.1/unused", monkeypatch, output)
    future = sa.Table("rag_future_not_in_0014", Base.metadata, sa.Column("id", sa.Integer, primary_key=True))
    try:
        command.upgrade(config, "0013_material_raw:head", sql=True)
    finally:
        Base.metadata.remove(future)
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
    assert "rag_future_not_in_0014" not in sql
    assert "DROP TABLE" not in sql
    assert "ALTER TABLE materials" not in sql
    assert "`end` > start" in sql, "MySQL reserved END identifier must be quoted in CHECK expressions"
