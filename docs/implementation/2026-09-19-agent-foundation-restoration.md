# Agent Foundation Restored; Terminal UI Removed

Date: 2026-09-19

## Corrected Scope

The user requested reversing the broad Web-only cleanup: retain the complete
Agent foundation and delete only the interactive terminal interface. This record
supersedes `2026-09-19-web-only-cleanup.md` and its plan.

Restored 162 non-UI files from local archive
`codex/archive-agent-before-web-cleanup-20260919`, commit
`0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`:

- `agents/`, `core/`: model clients, Agent execution and callback interfaces.
- `context/`, `memory/`: budgeting, deduplication, layered memory, summarization,
  recall, playbooks, conflict handling and persistence.
- `tools/`, `planning/`, `verify/`, `tdd/`, `workspace/`: all original capabilities,
  including Shell and file tools, retained as foundation code.
- `bench/`, `exp_hybrid_recall.py`, non-UI tests and fixtures.
- `learning/indexes.py`, `learning/vector_indexing.py`, `learning/db_cli.py`,
  `scripts/init_db.py` and the maintenance console entrypoint.

All original foundation production files match the archive after line-ending
normalization except `bench/default_factory.py`, whose import points to the new
non-interactive assembly module. Existing learning fixes and package renaming
remain. The Alembic `.env` configuration fix is also retained.

## UI Separation

The remaining 37 missing archive paths are `chat.py`, 18 files in `cli/`, and
18 terminal-only tests. Reusable code originally located inside `cli/` is kept:

- `core/runtime.py`: config loading/saving, storage isolation and `build_agent`.
  Terminal messages use logging; configuration persistence remains opt-in.
- `core/permissions.py`: permission decisions, session grants and the callback
  bridge; callers provide the approval interface.
- Four config/assembly tests now import `core.runtime`. Mixed permission tests
  retain the non-UI cases. New behavioral tests exercise assembly and callbacks
  without a terminal or live model.

Removed the interactive `knowpath` chat entrypoint. Retained both worker module
entrypoints, benchmark commands and `knowpath-learning-db`. Normal database setup
still uses Alembic: direct ORM initialization does not track migration revisions,
and the restored old vector indexing utility remains limited to graph version 1.

Restoring the foundation does not automatically integrate it into learning Web
requests. The Web pipeline still uses its learning services, retrieval and history
snapshots. It does not register Shell or file-editing tools.

## Dependencies and Data

The lockfile contains 70 packages versus 75 before cleanup. Only `prompt-toolkit`,
`rich`, `markdown-it-py`, `mdurl` and `wcwidth` are removed; retained package
versions are unchanged. Pytest remains a development dependency, and
`pymysql[rsa]` keeps MySQL authentication support explicit. `py/.venv` was synced.

No changes were made to user `.env`, tokens, databases, Docker volumes or running
PyCharm processes. Restored the optional generic Agent fields in `.env.example`;
Web model configuration remains separate. The normal Git index is unchanged;
this restoration is in the working tree and has not been committed or pushed.

## Verification

- Boundary regression before restoration: 3 failed, 3 passed, detecting missing
  foundation modules, maintenance entrypoint and script.
- New runtime regression first failed on the missing extraction module.
- Focused assembly, configuration, permissions and installation checks:
  **27 passed**.
- Full isolated Agent and Web suite: **1324 passed, 208 skipped, 12 failed**,
  with one existing Starlette/httpx deprecation warning. Dotenv and live provider
  keys were disabled; external database test configuration was cleared.
- All 12 failures reproduced on unmodified archived source in a separate temporary
  directory using the same interpreter. They are pre-existing Windows issues:
  5 path-separator expectations, 2 SQLite-handle cleanup failures, 3 tests requiring
  the `python3` shell command, and 2 tests lacking symlink privileges. None was
  deleted, weakened or marked xfail to conceal the result.
- Wheel built and installed into a fresh environment with runtime dependencies
  only. All **274 packaged Python files** parsed. Agent, Context, Memory, tools,
  verification and maintenance imports passed; terminal UI and its libraries were
  absent. Memory-mode API health, authentication, OpenAPI and both worker help
  commands passed without accessing live stores or providers.
- Archive comparison, production import scan and `git diff --check` passed.
  Independent read-only review found no actionable omission or regression.

The full suite is not entirely green because of the reproduced baseline failures.
External MySQL/Neo4j/Qdrant integration and paid model quality were not verified
by this restoration.
