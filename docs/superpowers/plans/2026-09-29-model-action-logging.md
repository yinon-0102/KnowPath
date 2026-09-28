# Model action logging

User approved the four follow-up fixes: RAG thread correlation, unified model events, parent/child action identifiers, and consistent usage/tool observations.

Use existing standard-library logging and RAG journals. Copy only observability metadata into fresh thread contexts; never copy SQL sessions or other ContextVars. Add span IDs to existing stages and one model_call_id per physical model attempt. Bridge request journals to the same event schema, retaining their timeout/unknown and validation semantics. Preserve all existing HTTP responses, model budgets/retry counts, leases, callbacks, tool approval/execution order and return values. Do not log prompts, reasoning, arguments, tool outputs, URLs or credentials. Missing usage is unknown, never zero or estimated billing. No new dependencies, migrations, automatic commits or production calls.

- [x] Reproduce and fix RAG thread correlation with concurrent/context-isolation tests.
- [x] Unify provider/RAG model action IDs, physical attempts, usage and validation events.
- [x] Observe Agent model/tool actions, approvals, parallel execution and timeout outcomes.
- [x] Document the event contract; run focused/full regression and independent review.

Implementation notes:
- Direct HTTP attempts and SDK invocations explicitly identify their observation scope. SDK internal retries remain opaque; unknown usage stays unknown.
- Parent spans, model/tool action IDs, RAG revision and stage counters are propagated without transferring database ContextVars.
- Supervised tool outcomes are settled by the waiting caller. Timeout cannot be replaced by a late worker return or exception; queued submitted actions are unknown on abort, never falsely cancelled.
- Independent review identified and reproduced both tool races with controlled scheduling tests, then both were corrected.
- The first full regression exposed an existing Alembic logging conflict: fileConfig disabled preloaded application loggers. A minimal ordered test reproduced the issue; migration startup now preserves host handlers and existing loggers. No schema or migration revisions changed.
- Final combined regression: 298 passed, one existing Starlette TestClient deprecation warning.
- Final full backend regression: 2729 passed, 263 skipped, one existing Starlette TestClient deprecation warning (266.34 seconds). Real dotenv loading and external test credentials were disabled; database tests used isolated temporary storage.
- Independent final review: no remaining blocker; 48 ordered regression tests passed, including adapter initialization before migration and the complete action-logging suite.
- Event semantics, correlation fields, deduplicated usage aggregation, privacy and SDK observation limits are documented in py/README.md. Git diff whitespace checks pass. No commit or push was performed for this task.

Follow-up publication: the user subsequently authorized module-based commits and remote push after completion. Combined pre-commit logging regression: 298 passed, one existing Starlette warning (17.86 seconds). Commit messages use English prefixes with Chinese descriptions; unrelated working-tree changes are excluded.
