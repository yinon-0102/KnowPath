from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from knowpath_backend.learning.db import Base, DatabaseSettings, init_db
from knowpath_backend.learning.materials import MaterialService
from knowpath_backend.learning.repositories import SqlAlchemyMaterialRepository


def test_database_schema_creates_core_learning_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    init_db(engine)
    tables = set(inspect(engine).get_table_names())

    assert {"materials", "material_versions", "source_chunks", "learning_spaces", "assessments", "evidence", "study_plans", "runs"}.issubset(tables)


def test_database_settings_use_documented_mysql_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = DatabaseSettings.from_env()

    assert settings.url.startswith("mysql+pymysql://")


def test_sqlalchemy_material_repository_round_trips_source_chunks():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    init_db(engine)
    service = MaterialService(SqlAlchemyMaterialRepository(engine))

    created = service.create(filename="notes.md", content=b"# Notes\n\nText", idempotency_key="db-1")
    loaded = service.repository.get_version(created.version.id)

    assert loaded is not None
    assert loaded.chunks[0].text == "Text"
