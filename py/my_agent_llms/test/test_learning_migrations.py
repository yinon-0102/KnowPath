"""Migration regressions use an isolated file database, never DATABASE_URL's target."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from my_agent_llms.learning.db import Base


@pytest.fixture
def migration_database(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    # env.py gives DATABASE_URL precedence over alembic.ini. Always override it
    # so a developer's local MySQL configuration cannot be touched by this test.
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    engine = sa.create_engine(database_url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _seed_run(engine):
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO runs (id, kind, status, progress, created_at) "
            "VALUES ('existing-run', 'export', 'queued', 0, '2026-09-18 00:00:00')"
        ))


def _assert_original_run(engine):
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT id, kind, status, progress FROM runs"
        )).all() == [("existing-run", "export", "queued", 0)]


def test_empty_database_upgrade_matches_current_schema(migration_database):
    config, engine = migration_database
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0002_run_events"
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    assert "run_events" in sa.inspect(engine).get_table_names()


def test_incremental_upgrade_and_downgrade_preserve_existing_runs(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0001_learning_schema")
    assert "run_events" not in sa.inspect(engine).get_table_names()
    _seed_run(engine)

    command.upgrade(config, "head")
    _assert_original_run(engine)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO run_events (run_id, sequence, event, created_at) "
            "VALUES ('existing-run', 1, 'run.started', '2026-09-18 00:00:00')"
        ))

    command.downgrade(config, "0001_learning_schema")
    assert "run_events" not in sa.inspect(engine).get_table_names()
    _assert_original_run(engine)
    command.upgrade(config, "head")
    _assert_original_run(engine)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM run_events")) == 0

    command.downgrade(config, "base")
    assert set(sa.inspect(engine).get_table_names()) <= {"alembic_version"}


def test_baseline_does_not_create_future_orm_tables(migration_database):
    config, engine = migration_database
    future = sa.Table("future_revision_only", Base.metadata,
                      sa.Column("id", sa.Integer(), primary_key=True))
    try:
        command.upgrade(config, "0001_learning_schema")
        tables = set(sa.inspect(engine).get_table_names())
        assert "materials" in tables
        assert "runs" in tables
        assert "run_events" not in tables
        assert "future_revision_only" not in tables
    finally:
        Base.metadata.remove(future)
