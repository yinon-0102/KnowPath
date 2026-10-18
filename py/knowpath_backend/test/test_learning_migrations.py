"""Migration regressions use an isolated file database, never DATABASE_URL's target."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from knowpath_backend.learning.persistence.db import Base


@pytest.mark.parametrize("environment_override", [False, True])
def test_migrations_load_backend_dotenv_without_overriding_environment(
    tmp_path, monkeypatch, environment_override,
):
    backend = Path(__file__).resolve().parents[2]
    config_path = tmp_path / "alembic.ini"
    config_path.write_text((backend / "alembic.ini").read_text(), encoding="utf-8")
    file_url = f"sqlite+pysqlite:///{(tmp_path / 'dotenv.db').as_posix()}"
    override_url = f"sqlite+pysqlite:///{(tmp_path / 'override.db').as_posix()}"
    fallback_url = f"sqlite+pysqlite:///{(tmp_path / 'fallback.db').as_posix()}"
    (tmp_path / ".env").write_text(f"DATABASE_URL={file_url}\n", encoding="utf-8")
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    (unrelated / ".env").write_text(f"DATABASE_URL={fallback_url}\n", encoding="utf-8")
    monkeypatch.chdir(unrelated)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if environment_override:
        monkeypatch.setenv("DATABASE_URL", override_url)
    config = Config(str(config_path))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", fallback_url.replace("%", "%%"))

    command.upgrade(config, "head")

    expected = override_url if environment_override else file_url
    assert config.get_main_option("sqlalchemy.url") == expected
    engine = sa.create_engine(expected)
    try:
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0021_space_scope_options"
    finally:
        engine.dispose()
    assert not (tmp_path / "fallback.db").exists()
    if environment_override:
        assert not (tmp_path / "dotenv.db").exists()


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
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0021_space_scope_options"
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    assert "run_events" in sa.inspect(engine).get_table_names()


def test_scope_options_migration_preserves_historical_scope_and_is_reversible(migration_database):
    from datetime import datetime
    config, engine = migration_database
    command.upgrade(config, '0020_material_parse_manifest')
    spaces = sa.Table('learning_spaces', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(spaces.insert().values(id='historical-space', name='Old', status='draft',
            bindings=[], topic_ids=['historical-topic'], excluded_topic_ids=[], profile={},
            space_version=7, scope_version=3, profile_version=2, state_version=4,
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2)))
        before = dict(connection.execute(sa.select(spaces)).mappings().one())
    command.upgrade(config, 'head')
    migrated = sa.Table('learning_spaces', sa.MetaData(), autoload_with=engine)
    assert 'scope_options' in migrated.c
    with engine.connect() as connection:
        after = dict(connection.execute(sa.select(migrated)).mappings().one())
    assert after.pop('scope_options') is None
    assert after == before
    command.downgrade(config, '0020_material_parse_manifest')
    restored = sa.Table('learning_spaces', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        assert dict(connection.execute(sa.select(restored)).mappings().one()) == before


def test_parser_identity_migration_preserves_legacy_chunks(migration_database):
    from datetime import datetime
    config, engine = migration_database
    command.upgrade(config, '0018_learning_personalization')
    meta = sa.MetaData()
    materials = sa.Table('materials', meta, autoload_with=engine)
    versions = sa.Table('material_versions', meta, autoload_with=engine)
    chunks = sa.Table('source_chunks', meta, autoload_with=engine)
    with engine.begin() as connection:
        timestamp = datetime(2026, 10, 1)
        connection.execute(materials.insert().values(id='legacy-material', name='Legacy', type='txt', status='ready',
            current_version_id='legacy-version', version=1, size_bytes=3, created_at=timestamp, updated_at=timestamp))
        connection.execute(versions.insert().values(id='legacy-version', material_id='legacy-material', filename='old.txt',
            media_type='text/plain', content_hash='a'*64, size_bytes=3, status='ready', graph_version=1, created_at=timestamp))
        connection.execute(chunks.insert().values(id='legacy-chunk', material_version_id='legacy-version', text='old',
            section_path=['Legacy'], line_start=1, line_end=1, content_hash='b'*64))
        previous = connection.execute(sa.select(chunks)).mappings().all()
    command.upgrade(config, 'head')
    with engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT parser_version FROM material_versions')) == 'legacy_unknown'
        assert connection.scalar(sa.text('SELECT parse_manifest FROM material_versions')) is None
        assert connection.execute(sa.select(chunks)).mappings().all() == previous

def test_lesson_incremental_migration_preserves_prior_records(migration_database):
    config,engine=migration_database
    command.upgrade(config,'0016_learning_notes')
    _seed_run(engine)
    assert 'study_lessons' not in sa.inspect(engine).get_table_names()
    command.upgrade(config,'head')
    _assert_original_run(engine)
    assert 'study_lessons' in sa.inspect(engine).get_table_names()
    command.downgrade(config,'0016_learning_notes')
    _assert_original_run(engine)
    assert 'study_lessons' not in sa.inspect(engine).get_table_names()
    assert 'note_chapters' in sa.inspect(engine).get_table_names()


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


def test_space_profile_backfill_preserves_existing_metadata(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0002_run_events")
    metadata = sa.MetaData()
    spaces = sa.Table("learning_spaces", metadata, autoload_with=engine)
    from datetime import datetime
    with engine.begin() as connection:
        connection.execute(spaces.insert().values(
            id="existing-space", name="Existing", status="active", goal="Learn functions",
            target_date="2026-10-01", weekly_minutes=180, bindings=[], topic_ids=[],
            excluded_topic_ids=[], space_version=7, scope_version=3, profile_version=2,
            state_version=4, created_at=datetime(2026, 9, 17), updated_at=datetime(2026, 9, 18)))
    command.upgrade(config, "head")
    spaces = sa.Table("learning_spaces", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(spaces)).mappings().one()
        assert row["space_version"] == 7
        assert row["profile_version"] == 2
        assert row["state_version"] == 4
        assert row["profile"]["goal"] == {"value": "Learn functions", "source": "explicit", "updated_at": "2026-09-18T00:00:00+00:00"}
        assert row["profile"]["weekly_minutes"]["value"] == 180
        assert row["profile"]["target_date"]["value"] == "2026-10-01"
    command.downgrade(config, "0002_run_events")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT goal FROM learning_spaces")) == "Learn functions"
    command.upgrade(config, "head")


def test_exports_upgrade_and_downgrade_preserve_existing_data(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0005_plans_sessions")
    _seed_run(engine)
    command.upgrade(config, "head")
    assert "learning_exports" in sa.inspect(engine).get_table_names()
    _assert_original_run(engine)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(config, "0005_plans_sessions")
    assert "learning_exports" not in sa.inspect(engine).get_table_names()
    _assert_original_run(engine)


def test_material_version_backfill_preserves_existing_rows(migration_database):
    from datetime import datetime
    config, engine = migration_database
    command.upgrade(config, "0006_exports")
    table = sa.Table("materials", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id="existing-material", name="Notes", type="md", status="archived",
            current_version_id=None, size_bytes=42, created_at=datetime(2026, 9, 18), updated_at=datetime(2026, 9, 18)))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT name, status, size_bytes, version FROM materials")).one() == ("Notes", "archived", 42, 1)
    command.downgrade(config, "0006_exports")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT name, status, size_bytes FROM materials")).one() == ("Notes", "archived", 42)


def test_assessment_context_backfill_preserves_result_and_snapshot(migration_database):
    from datetime import datetime
    config, engine = migration_database
    command.upgrade(config, "0007_material_version")
    meta = sa.MetaData()
    spaces = sa.Table("learning_spaces", meta, autoload_with=engine)
    assessments = sa.Table("assessments", meta, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(spaces.insert().values(id="audit-space", name="Audit", status="active",
            bindings=[], topic_ids=[], excluded_topic_ids=[], space_version=1, scope_version=1,
            profile_version=1, state_version=0, profile={}, created_at=datetime(2026, 9, 18), updated_at=datetime(2026, 9, 18)))
        connection.execute(assessments.insert().values(id="audit-assessment", space_id="audit-space",
            kind="diagnostic", status="completed", topic_ids=[], questions=[], snapshot={"frozen": True},
            result={"state_version": 7}, created_at=datetime(2026, 9, 18)))
    command.upgrade(config, "head")
    assessments = sa.Table("assessments", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(assessments)).mappings().one()
        assert row["context"] == {}
        assert row["snapshot"] == {"frozen": True}
        assert row["result"] == {"state_version": 7}
    command.downgrade(config, "0007_material_version")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT id FROM assessments")) == "audit-assessment"


def test_correction_context_backfill_preserves_audit(migration_database):
    from datetime import datetime
    config, engine = migration_database
    command.upgrade(config, '0011_graph_preparation')
    meta = sa.MetaData()
    spaces = sa.Table('learning_spaces', meta, autoload_with=engine)
    corrections = sa.Table('knowledge_corrections', meta, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(spaces.insert().values(id='correction-space', name='Audit', status='active',
            bindings=[], topic_ids=[], excluded_topic_ids=[], space_version=1, scope_version=0,
            profile_version=1, state_version=0, profile={}, created_at=datetime(2026, 9, 19), updated_at=datetime(2026, 9, 19)))
        connection.execute(corrections.insert().values(id='old-correction', space_id='correction-space', kind='node',
            target_id='old-topic', action='reject', reason='preserve audit', status='pending', created_at=datetime(2026, 9, 19)))
    command.upgrade(config, 'head')
    corrections = sa.Table('knowledge_corrections', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(corrections)).mappings().one()
        assert row['context'] == {}
        assert row['reason'] == 'preserve audit'
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(config, '0011_graph_preparation')
    with engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT reason FROM knowledge_corrections')) == 'preserve audit'
