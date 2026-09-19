# Web-only Backend Cleanup

> Superseded by the user's corrected scope: retain the complete Agent foundation
> and remove only terminal UI. The results below describe the earlier, broader
> cleanup, not the current tree. See [restoration plan](2026-09-19-agent-foundation-plan.md).

Date: 2026-09-19

## Boundary

The current product is the KnowPath Web backend under `py/knowpath_backend/learning`.
The API and both workers remain, together with learning tests, Alembic migrations,
the empty-database SQL snapshot, Docker configuration and PyCharm launch profiles.
`frontend/` remains reserved; this cleanup does not implement a frontend.

Removed the unused terminal Agent, its runtime, Shell/file-editing tools, benchmark
code, experimental scripts and dedicated tests. Also retired unused
`learning/indexes.py`, the fixed-version `vector_indexing.py`, `db_cli.py` and
`scripts/init_db.py`. The two worker `*_cli.py` modules are production process
entrypoints and are intentionally retained.

## Archive

Before deletion, the working source including prior uncommitted fixes and the
package rename was archived in a separate local Git branch:

- Branch: `codex/archive-agent-before-web-cleanup-20260919`
- Commit: `0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`
- 368 archived files; every removed source/resource file was checked against it.

The archive excludes local secrets, virtual environments and caches. Creating it
did not switch the active branch or modify the normal Git index. It has not been
pushed. Current cleanup changes remain in the working tree. Keel's original MIT
LICENSE and attribution remain.

## Dependencies and Setup

The lockfile shrank from 75 to 47 packages without upgrading retained versions.
Removed SDKs and terminal libraries have no surviving backend consumers. Pytest
is now a development dependency. `pymysql[rsa]` explicitly retains cryptography
support for MySQL 8.4 authentication. The existing `py/.venv` was synchronized.

Normal database initialization and upgrades use Alembic. It now loads `.env`
beside `alembic.ini`, preserving process environment precedence; configuration
regressions use temporary SQLite databases. No user database was migrated or
modified by this cleanup. Local `.env`, tokens, database names and Docker volumes
were preserved. README, architecture and API module mappings describe the current
Web services; earlier implementation reports are marked as historical.

## Verification

- Boundary tests before removal: 3 failed, 2 passed, detecting obsolete entrypoints,
  modules and the duplicate initialization script.
- New migration configuration tests before the fix: 1 failed, 1 passed, reproducing
  the missing `.env` load and confirming environment precedence already worked.
- Full remaining backend suite after cleanup: **827 passed, 208 skipped**, with
  one existing Starlette/httpx deprecation warning. `.env` loading was disabled,
  provider keys and external test configuration were cleared, and only memory or
  temporary SQLite stores were used. Migration upgrades, downgrades and schema
  parity passed as part of this run.
- Offline wheel build succeeded. Installed it in a fresh temporary environment
  using locked runtime dependencies only, with no pytest or old SDKs. API import,
  memory-mode health, authentication, OpenAPI and both worker `--help` commands
  passed. All 116 packaged Python files parsed; retired packages, adapters and
  console entrypoints were absent. PyMySQL RSA support was present.
- Independent read-only review found no actionable issues in dependency removal,
  retained imports, migration configuration or startup documentation.

External MySQL/Neo4j/Qdrant tests were skipped and no paid model requests were made.
This verifies the cleanup and package boundary, not live provider quality or the
current contents of the user's running stores. Existing PyCharm processes were
not restarted; restart them before checking the new code in a running service.
