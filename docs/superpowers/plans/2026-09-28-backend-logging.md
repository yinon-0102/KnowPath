# Backend Logging Implementation Plan

**Goal:** Make API, model and graph worker operations diagnosable from correlated, bounded and privacy-conscious logs.

**Architecture:** Retain Python standard logging. Production entrypoints configure readable stderr and optional rotating JSONL files once per process. A shared context follows a request through ASGI streaming and threadpool work; job logs use persisted Run/outbox identifiers. Factories do not reconfigure global handlers. Logging never changes response bodies, grading, leases or task outcomes.

**Defaults:** INFO, readable console, 10 MiB JSONL files with five backups per process under py/logs; environment overrides support JSON console, DEBUG and console-only operation. Health/SSE polling is DEBUG on success; errors remain visible. File names distinguish API/model/graph workers and PIDs to avoid shared rollover. Logs are diagnostics, not a transactional business audit.

**Privacy:** New events contain IDs, enums, counts, durations and safe error codes. Never collect request bodies, headers, queries, source text, prompts, answers, credentials or lease tokens. Exceptions retain types and stack locations, without messages, locals or source lines. Legacy text receives credential redaction; third-party verbose logs remain suppressed independently of application DEBUG.

## Execution
- [x] RED: API correlation, failures and stream lifecycle; structured formatting, nested redaction, bounded exception frames, context isolation, idempotent setup and file rotation.
- [x] GREEN: shared events/configuration and request middleware; preserve existing HTTP and SSE contracts.
- [x] RED/GREEN: model retries, worker attempts/settlement, RAG stages and process lifecycle; preserve CLI --once JSON stdout.
- [x] Document event fields, configuration, investigation examples and process-file retention; ignore generated logs without changing existing root ignore edits.
- [x] Run focused suites, complete backend regression and independent code review. Record exact verification and limitations.

Validation uses the existing virtualenv with PYTHON_DOTENV_DISABLED=1 and PYTHONDONTWRITEBYTECODE=1, cleared external-service test toggles/connection variables, and unique basetemp outside Git. No live services, production configuration, new dependencies or deployment are required. Existing unrelated work remains intact. This request implements logging; it does not authorize a new automatic commit or push.

## Verification results

- Focused backend regression: 295 passed, 12 skipped (37.53 s); subsequent logging edge/handoff tests: 41 passed (3.25 s).
- Isolated production entrypoint smoke test: HTTP health 200, API build/start/shutdown lifecycle events emitted as JSON, local token absent.
- Independent read-only review identified Uvicorn preformatted traceback, pypdf content warning, and quoted-credential redaction gaps. Added failing regressions and fixed all three.
- Added worker cleanup diagnostics with nonzero exit on cleanup failure; retained cleanup ordering and machine-readable stdout.
- Final full backend regression: **2694 passed, 263 skipped, 1 existing Starlette TestClient deprecation warning**, 252.00 seconds. Command: ./.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider --basetemp=C:/Users/27202/.codex/tmp/kp-log-full-final-15 knowpath_backend/test (cwd: py; isolation environment described above). No external services, real secrets or real .env loaded.
- The first full run exposed a test-order assumption that treated existing application-owned handlers as foreign. Corrected that assertion; reverse-order regression: 59 passed, 12 skipped. The fresh full rerun above passes.
- Independent follow-up review: all three privacy findings resolved; actual Uvicorn lifespan reproduction no longer exposes private content. Reviewer independently confirmed all 41 logging tests pass and found no remaining definite issue in scope.
- git diff --check passes; only py/logs/.gitignore is unignored in the generated log directory. No commits, pushes, deployments or dependency changes were performed.
- Limits: live provider/container integration was not exercised; logs are diagnostic rather than transactional audit; old PID log files require external retention policy; host-owned handlers keep their own formatting/privacy responsibility.

Follow-up publication: the user subsequently authorized module-based commits and remote push after completion. Combined pre-commit logging regression: 298 passed, one existing Starlette warning. Unrelated working-tree files are excluded.
