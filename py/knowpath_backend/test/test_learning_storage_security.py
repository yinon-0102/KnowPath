"""Storage configuration checks never connect to the developer's services."""

from pathlib import Path
from io import StringIO
import re

from dotenv import dotenv_values, set_key
import httpx
import pytest
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config

from knowpath_backend.learning.indexes import QdrantVectorStore
from knowpath_backend.learning.persistence.db import DatabaseSettings
from knowpath_backend.learning.storage import GraphSettings, VectorSettings


ROOT = Path(__file__).resolve().parents[3]


def _service(name):
    compose = (ROOT / "infra/docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^volumes:|\Z)", compose, re.M | re.S)
    assert match, f"Missing service: {name}"
    return match.group(1)


@pytest.mark.parametrize("service", ["mysql", "neo4j", "qdrant", "minio"])
def test_storage_ports_only_bind_loopback(service):
    body = _service(service)
    ports = re.search(r"^    ports:\n(.*?)(?=^    \S|\Z)", body, re.M | re.S)
    assert ports
    mappings = re.findall(r'^      - "([^"]+)"$', ports.group(1), re.M)
    assert mappings
    assert all(port.startswith("127.0.0.1:") for port in mappings)
    assert "network_mode: host" not in body


@pytest.mark.parametrize("service,setting,variable", [
    ("mysql", "MYSQL_PASSWORD", "MYSQL_PASSWORD"),
    ("mysql", "MYSQL_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"),
    ("neo4j", "NEO4J_AUTH", "NEO4J_PASSWORD"),
    ("qdrant", "QDRANT__SERVICE__API_KEY", "QDRANT_API_KEY"),
    ("minio", "MINIO_ROOT_PASSWORD", "MINIO_ROOT_PASSWORD"),
])
def test_compose_requires_nonempty_external_secrets(service, setting, variable):
    value = re.search(rf"^      {setting}: (.+)$", _service(service), re.M)
    assert value, f"Missing {setting} authentication setting"
    assert "${" + variable + ":?" in value.group(1)


def test_example_files_do_not_distribute_storage_passwords():
    infra = dotenv_values(ROOT / "infra/.env.example", interpolate=False)
    backend = dotenv_values(ROOT / "py/.env.example", interpolate=False)
    for name in ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "NEO4J_PASSWORD", "QDRANT_API_KEY", "MINIO_ROOT_PASSWORD"):
        assert name in infra
        assert not infra[name]
    for name in ("DATABASE_URL", "NEO4J_PASSWORD", "QDRANT_API_KEY", "MINIO_SECRET_KEY"):
        assert name in backend
        assert not backend[name]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_database_settings_reject_unconfigured_url(monkeypatch, value):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if value is not None:
        monkeypatch.setenv("DATABASE_URL", value)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        DatabaseSettings.from_env()


def test_database_settings_preserve_explicit_sqlite(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    assert DatabaseSettings.from_env().url == "sqlite+pysqlite:///:memory:"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_graph_settings_reject_unconfigured_password(monkeypatch, value):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    if value is not None:
        monkeypatch.setenv("NEO4J_PASSWORD", value)
    with pytest.raises(ValueError, match="NEO4J_PASSWORD is required"):
        GraphSettings.from_env()


def test_vector_settings_load_api_key(monkeypatch):
    monkeypatch.setenv("QDRANT_API_KEY", "test-only-vector-secret")
    assert VectorSettings.from_env().api_key == "test-only-vector-secret"


def test_vector_store_sends_api_key_to_qdrant(monkeypatch):
    import qdrant_client

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"result": {"collections": []}, "status": "ok", "time": 0})

    client_type = qdrant_client.QdrantClient

    def local_transport_client(**kwargs):
        return client_type(**kwargs, transport=httpx.MockTransport(respond), check_compatibility=False)

    monkeypatch.setattr(qdrant_client, "QdrantClient", local_transport_client)
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", "test-only-vector-secret")
    store = QdrantVectorStore()
    try:
        assert store.client.get_collections().collections == []
        assert requests[0].headers.get("api-key") == "test-only-vector-secret"
    finally:
        store.client.close()


def _sync_script():
    readme = (ROOT / "infra/README.md").read_text(encoding="utf-8")
    return re.search(r"@'\n(.*?)\n'@", readme, re.S).group(1)


def _sync_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "infra").mkdir()
    (tmp_path / "py").mkdir()
    source = tmp_path / "infra/.env"
    for key in ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "NEO4J_PASSWORD",
                "QDRANT_API_KEY", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        set_key(str(source), key, "test-only-secret", quote_mode="always")
    (tmp_path / "py/.env").write_text("LEARNING_LOCAL_TOKEN=keep-existing-token\n", encoding="utf-8")
    return source


def test_documented_sync_escapes_mysql_password_and_preserves_settings(tmp_path, monkeypatch, capsys):
    source = _sync_source(tmp_path, monkeypatch)
    password = "test:p@ss/word%# 'quoted' $literal"
    set_key(str(source), "MYSQL_PASSWORD", password, quote_mode="always")
    exec(compile(_sync_script(), "infra/README.md", "exec"), {})
    values = dotenv_values(tmp_path / "py/.env")
    assert make_url(values["DATABASE_URL"]).password == password
    assert values["LEARNING_LOCAL_TOKEN"] == "keep-existing-token"
    assert values["QDRANT_API_KEY"] == "test-only-secret"
    assert password not in capsys.readouterr().out


def test_documented_sync_rejects_secret_interpolation_before_writing(tmp_path, monkeypatch):
    source = _sync_source(tmp_path, monkeypatch)
    set_key(str(source), "NEO4J_PASSWORD", "test-${UNSET_PLACEHOLDER}-secret", quote_mode="always")
    target = tmp_path / "py/.env"
    before = target.read_bytes()
    with pytest.raises(SystemExit, match="interpolation"):
        exec(compile(_sync_script(), "infra/README.md", "exec"), {})
    assert target.read_bytes() == before


def _migration_config(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = Config(str(ROOT / "py/alembic.ini"), output_buffer=StringIO())
    config.set_main_option("script_location", str(ROOT / "py/migrations"))
    return config


def test_shipped_migration_config_contains_no_database_credentials(monkeypatch):
    assert not _migration_config(monkeypatch).get_main_option("sqlalchemy.url")


def test_migrations_require_explicit_database_without_opening_connection(monkeypatch):
    config = _migration_config(monkeypatch)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        command.upgrade(config, "base", sql=True)


def test_migrations_accept_explicit_sqlite_config(monkeypatch):
    config = _migration_config(monkeypatch)
    config.set_main_option("sqlalchemy.url", "sqlite+pysqlite:///:memory:")
    command.upgrade(config, "base", sql=True)
    assert config.get_main_option("sqlalchemy.url") == "sqlite+pysqlite:///:memory:"
