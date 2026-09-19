# Agent Foundation Restoration Plan

**Goal:** Undo the broad Web-only cleanup and retain all Agent foundation code,
removing only the interactive terminal interface and its dedicated tests.

**Architecture:** Restore non-UI source from archive
`0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`. Move reusable configuration, Agent
assembly and permission decisions out of `cli/` into `core/`. Preserve the current
Web services, package rename, migration configuration fix and all local data.

**Tech Stack:** Python, existing Agent/Memory/Context/Tool/Verify modules, FastAPI,
uv and pytest. No new Web integration is implied by restoring the foundation.

## Tasks

- [x] Update boundary regressions to require the full foundation and maintenance
  tools while rejecting the terminal UI entrypoint; verify failure first.
- [x] Restore all non-UI modules, experimental/benchmark code and tests from the
  archive. Preserve current learning fixes and configuration files.
- [x] Extract reusable Agent assembly/configuration and permissions from the old
  CLI, rewire benchmark and retained tests, and remove all CLI import consumers.
- [x] Restore dependencies and non-interactive maintenance entrypoints, remove
  only terminal UI dependencies, refresh lockfile and synchronize `py/.venv`.
- [x] Correct active docs and mark prior broad-cleanup reports as superseded.
- [x] Run isolated foundation/Web regressions and package validation; compare
  any restored baseline failures with the archive and report remaining limits.
- [x] Obtain a read-only review and record the final retained/deleted boundary.

Results: [restoration record](2026-09-19-agent-foundation-restoration.md).
