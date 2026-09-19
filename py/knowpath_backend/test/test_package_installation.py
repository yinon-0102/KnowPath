"""Installed-package regressions: import paths, console scripts and migrations."""

import importlib
from importlib.metadata import distribution
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_backend_package_and_service_modules_are_importable():
    for name in (
        "knowpath_backend",
        "knowpath_backend.learning.api",
        "knowpath_backend.learning.graph_worker_cli",
        "knowpath_backend.learning.model_worker_cli",
        "knowpath_backend.learning.db_cli",
        "knowpath_backend.cli.app",
    ):
        assert importlib.import_module(name).__name__ == name


def test_installed_backend_exposes_loadable_console_scripts():
    package = distribution("knowpath-backend")
    scripts = {entry.name: entry for entry in package.entry_points
               if entry.group == "console_scripts"}
    assert scripts["knowpath"].value == "knowpath_backend.cli.app:main"
    assert scripts["knowpath-learning-db"].value == "knowpath_backend.learning.db_cli:main"
    assert callable(scripts["knowpath"].load())
    assert callable(scripts["knowpath-learning-db"].load())


def test_alembic_revision_modules_load_with_renamed_package():
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())
    assert revisions
    assert len(scripts.get_heads()) == 1
    assert all(callable(revision.module.upgrade) for revision in revisions)


def test_database_init_script_delegates_to_package_entrypoint(monkeypatch):
    import runpy
    from knowpath_backend.learning import db_cli

    called = []
    monkeypatch.setattr(db_cli, "main", lambda: called.append(True))
    backend = Path(__file__).resolve().parents[2]
    runpy.run_path(str(backend / "scripts" / "init_db.py"), run_name="__main__")
    assert called == [True]
