# Backend package rename

> Historical rename record. The generic Agent foundation, maintenance command and
> database compatibility script are retained; only the terminal `knowpath` chat
> command and UI are removed. Current setup: [backend README](../../py/README.md).

Date: 2026-09-19

## Names and installation

- Python package: `knowpath_backend`.
- Distribution: `knowpath-backend`.
- Console scripts: `knowpath` and `knowpath-learning-db`.
- Backend working directory: `py/`; interpreter: `py/.venv/Scripts/python.exe`.

The package directory, absolute imports, dynamic imports, test patch targets,
Alembic environment, scripts, build configuration and documentation now use the
new package. Run `uv sync --locked` from `py/` after updating an existing checkout.
The lockfile changes only the project's distribution name; dependency versions
are unchanged. Restart previously running API and worker processes with the new
module paths:

```powershell
uv run uvicorn knowpath_backend.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m knowpath_backend.learning.graph_worker_cli
uv run python -m knowpath_backend.learning.model_worker_cli
```

Existing database names, collection names and Docker volumes retain their names;
this change does not migrate or replace stored data. Historical Keel attribution
and existing API response identifiers are outside the Python package rename.
The database initialization compatibility script now delegates directly to the
package CLI instead of shadowing it with an invalid local function.

## Verification

- The new import/distribution checks failed before implementation (2 failed,
  1 passed), then passed after reinstallation.
- Package and Alembic migration tests: 11 passed before adding the script
  regression; the final 4 package/entrypoint checks all passed.
- The database script regression reproduced its pre-existing `NameError`, then
  passed after correcting delegation; the test does not access a database.
- Full backend regression, including real MySQL/Neo4j/Qdrant fixtures:
  **1633 passed, 38 skipped, 16 failed**, with one pre-existing Starlette warning.
- All 16 failures were reproduced against the unchanged pre-rename Git HEAD in
  an isolated temporary checkout. They are not import or package lookup errors.
- A wheel was built and loaded directly outside the source tree; API, graph
  worker, model worker and CLI imports resolved from the wheel. All 307 packaged
  Python files parsed successfully.
- The editable installation also imports outside the source tree, exposes both
  new console scripts, and no longer resolves the previous package name.
- Both worker `--help` commands succeed without claiming jobs.
- A repository scan found no remaining previous-package import/path references
  in source, project configuration or documentation.

The 38 skips are fixture variants inapplicable to their selected backend; the
real MySQL variants were enabled. Tests used deterministic model adapters with
model keys cleared and dotenv loading disabled. Test artifacts and build output
were placed in the operating system temporary directory.

## Existing full-suite failures

These failures were reproduced before and after the rename on this Windows host:

- Terminal colour expectations: `test_approval_box`, `test_cli_chat_view`,
  `test_todo_panel` (3).
- POSIX vs Windows output paths: `test_glob`, `test_grep` (2), `test_list_dir`,
  `test_workspace::test_relative_strips_root` (5).
- No console screen buffer in the test process: `test_live_session_ordering` (1).
- Open SQLite handles during temporary-directory removal: `test_playbook` (2).
- Tests assuming a `python3` shell command: `test_verify_skip_bad_oracle` (3).
- Missing Windows symlink privileges: `test_workspace` symlink tests (2).

This rename does not claim an entirely passing legacy CLI test suite. Existing
platform compatibility issues remain separate from package import validation.
