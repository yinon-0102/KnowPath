# Web-only Backend Implementation Plan

**Goal:** Deliver the learning Web backend without the unused terminal Agent.

**Architecture:** Keep `knowpath_backend.learning`, API and worker entrypoints,
Alembic migrations and learning regression tests. Archive the existing working
tree in a local Git branch before removing the legacy runtime. Preserve user
configuration, storage names, databases, credentials and copyright attribution.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy/Alembic, MySQL, Neo4j, Qdrant,
DashScope, uv, pytest.

## Tasks

- [ ] Archive the current source and configuration in a `codex/` Git branch,
  excluding secrets, environments and caches; leave the normal index untouched.
- [ ] Update package regression tests to require Web imports, prohibit legacy
  runtime modules and retire the legacy console commands; verify failure first.
- [ ] Remove legacy UI/runtime/tools/evaluations and only their dedicated tests.
  Remove early unused index adapters, fixed-version index command and duplicate
  database initialization commands. Keep migration tooling and test schema setup.
- [ ] Trim dependencies, refresh the lockfile without dependency upgrades, and
  synchronize the existing backend environment.
- [ ] Update architecture, startup docs and environment template to reflect the
  Web-only boundary. Preserve historical implementation records as history.
- [ ] Verify learning tests on isolated stores, package construction/imports,
  worker help commands, migration tests and the absence of legacy references.
  Do not run workers or write integration fixtures to the user's active database.
- [ ] Obtain a read-only review, resolve findings, and document results and the
  archive reference. Leave the existing virtual environment and `.env` intact.
