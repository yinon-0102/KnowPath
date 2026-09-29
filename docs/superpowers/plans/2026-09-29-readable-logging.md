# Readable logging implementation plan

**Goal:** Explain backend activity in plain Chinese while retaining structured diagnostics.

**Design:** Render existing sanitized events through a shared presentation module. Text output leads with what happened and selectively shows meaningful counts, duration and correlation IDs. JSONL retains its existing fields and gains an additive human-readable message. No model calls, dependencies, business-flow changes or automatic commits.

**Alternatives:** Translating every field still overwhelms readers; removing diagnostics impairs troubleshooting. Use concise summaries plus existing JSON diagnostics instead.

**Scope:** API lifecycle/requests, commands, background jobs, model/tool actions, material parsing, knowledge graph preparation and retrieval/answer stages. Unknown events receive a neutral fallback with their original event identifier. Unknown outcomes, partial model usage and pending transaction requests must stay explicitly uncertain.

- [x] Add failing formatter regressions in `py/knowpath_backend/test/test_readable_logging.py`: plain summaries, outcome truthfulness, privacy, unknown events, JSON compatibility and handler integration.
- [x] Add `py/knowpath_backend/observability/presentation.py` and connect it in `configuration.py`; generate descriptions only from sanitized event metadata.
- [x] Update `py/README.md` with rendered examples, message semantics and how to retrieve full diagnostics.
- [x] Run new tests, existing logging/action regressions and relevant backend integration tests; inspect actual text/JSON output and `git diff --check`.

**Verification:** Use `py/.venv/Scripts/python.exe -m pytest` with dotenv disabled, bytecode disabled and external-service test toggles removed. Preserve unrelated working-tree changes.

## Verification results

- Confirmed failing tests before implementation: 32 missing-description assertions; follow-up purpose/stop-reason coverage: 13 expected failures; cancellation wording: 1 expected failure.
- Latest focused regression: **135 passed**, one existing Starlette TestClient deprecation warning. Includes 59 new presentation cases plus existing logging/action suites.
- Independent read-only review checked producer semantics, privacy and field compatibility; no delivery blocker. Independent presentation suite: **59 passed**.
- Inspected rendered retrieval, worker retry, model completion, tool timeout and mid-stream HTTP failure examples. `git diff --check` passes.
- Full backend regression: **2787 passed, 263 skipped**, one existing Starlette TestClient deprecation warning (267.76 seconds). External-service test toggles/credentials were cleared and real dotenv loading was disabled. The cancellation wording added after full-suite collection is covered by the latest 135-case focused run and the independent 59-case presentation run.
- Final changes are limited to presentation, formatter integration, tests, backend documentation and this plan. No event producers, business logic, dependencies, credentials or deployments changed. No commit or push performed.
