"""Installed-package regressions: import paths, console scripts and migrations."""

import importlib
import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import os
import subprocess
import sys

import pytest

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_backend_package_and_service_modules_are_importable():
    for name in (
        "knowpath_backend",
        "knowpath_backend.learning.api",
        "knowpath_backend.learning.graph_worker_cli",
        "knowpath_backend.learning.model_worker_cli",
        "knowpath_backend.learning.workers.graph_cli",
        "knowpath_backend.learning.workers.model_cli",
    ):
        assert importlib.import_module(name).__name__ == name


def test_installed_backend_exposes_maintenance_but_not_terminal_chat():
    package = distribution("knowpath-backend")
    scripts = {entry.name: entry for entry in package.entry_points
               if entry.group == "console_scripts"}
    assert "knowpath" not in scripts
    assert scripts["knowpath-learning-db"].value == "knowpath_backend.learning.db_cli:main"


def test_agent_foundation_and_maintenance_modules_are_retained():
    for suffix in (
        "agents", "core", "memory", "context", "planning", "verify",
        "tdd", "workspace", "tools", "bench", "learning.db_cli",
        "learning.vector_indexing", "learning.indexes",
    ):
        assert importlib.import_module("knowpath_backend." + suffix) is not None, suffix


def test_terminal_chat_interface_is_removed():
    assert importlib.util.find_spec("knowpath_backend.cli") is None
    backend = Path(__file__).resolve().parents[2]
    assert not (backend / "chat.py").exists()


def test_alembic_revision_modules_load_with_renamed_package():
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())
    assert revisions
    assert len(scripts.get_heads()) == 1
    assert all(callable(revision.module.upgrade) for revision in revisions)


def test_database_initialization_uses_migrations_and_empty_database_sql():
    backend = Path(__file__).resolve().parents[2]
    assert (backend / "alembic.ini").is_file()
    assert (backend / "init_mysql.sql").is_file()
    assert (backend / "scripts" / "init_db.py").is_file()


@pytest.mark.parametrize("entrypoint", [
    "knowpath_backend.learning.graph_worker_cli",
    "knowpath_backend.learning.model_worker_cli",
    "knowpath_backend.learning.vector_indexing",
])
def test_public_worker_and_index_commands_support_module_startup(entrypoint, tmp_path):
    backend = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=str(backend), PYTHON_DOTENV_DISABLED="1")
    result = subprocess.run(
        [sys.executable, "-m", entrypoint, "--help"], cwd=tmp_path,
        env=environment, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
