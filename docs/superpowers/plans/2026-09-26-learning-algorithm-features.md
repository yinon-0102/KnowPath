# Learning Algorithm Features Implementation Plan

> For agentic workers: use executing-plans/TDD; independently owned modules follow dispatching-parallel-agents and receive integration review.

**Goal:** Deliver the four user-approved learning algorithm features with usable API/UI surfaces and honest evaluation.

**Architecture:** Reuse immutable question/evidence snapshots and existing transaction boundaries. Keep selection, review scheduling, budget comparison and historical replay in focused versioned modules; root owns application wiring, API snapshots and UI integration. Preserve previous repairs and unrelated user changes.

**Tech Stack:** Python/FastAPI, SQLAlchemy memory/SQLite adapters, pytest, existing vanilla JavaScript frontend and Node tests.

Spec: docs/superpowers/specs/2026-09-26-learning-algorithm-features-design.md. User authorized implementation and subsequent modular local commits; no additional approval or deployment step is necessary.

## A. Adaptive diagnostics
Files: assessments/adaptive.py, assessments/service.py, assessments/schemas.py, optionally assessments/generation.py; api/routers/diagnostics.py; test/test_learning_adaptive_diagnostics.py.
- [x] RED: opt-in hides future questions, prioritizes weak topics, wrong independent answer selects scoped prerequisite, assisted/unverified observation cannot confirm a prerequisite gap, future/repeated/out-of-order answer is rejected, replay/restart does not advance twice.
- [x] Implement deterministic selection and durable snapshot trace. Public diagnostic view exposes current question/reasons only. Keep legacy adaptive=false behavior exactly.
- [x] Freeze prior states and used families; add difficulty and assessment_kind metadata to finalized evidence for scheduling/reporting (no numerical fabricated scores).
- [x] GREEN: focused adaptive tests plus existing assessment/source/grade/question-review regressions.

## B. Review scheduling and evolution
Files: assessments/review_policy.py, assessments/mastery.py, evolution/service.py, api/routers/evolution.py; plans/service.py only its planning-state projection; test/test_learning_review_evolution.py.
- [x] RED: independent spaced success lengthens review, independent wrong answer shortens, assistance/regrade cannot move observation time, easy-only evidence cannot masquerade as hard/application success, same due timestamp in state/planner/evolution.
- [x] Implement pure review_schedule(evidence, *, revision_id, epoch, as_of) with versioned explanation; include stable stage/interval/due/selected IDs.
- [x] Implement read-only evolution with explicit observation/revision/reset events and evidence sufficiency. Supply root integration helper for AssessmentService.state instead of editing A-owned service.py.
- [x] GREEN: mastery, review/evolution, planner cycle and knowledge-update suites.

## C. Budget plan comparison
Files: plans/comparison.py, plans/comparison_schemas.py, api/routers/plan_comparisons.py; test/test_learning_plan_comparisons.py.
- [x] RED: 20/40-minute scenarios share snapshot, each daily/weekly cap holds, prerequisites precede dependents, missing prerequisites stay blocked, deadline limits horizon, no changes to real tasks/evidence, idempotent retry.
- [x] Implement deterministic dependency-closure scheduling and explicit selection/defer reasons; label prioritization as heuristic coverage, not predicted gain.
- [x] GREEN: comparison memory/SQLite persistence/isolation and existing plan constraints.

## D. Replay and evaluation (root)
Files: evaluation/replay.py, evaluation/schemas.py, api/routers/policy_replays.py; test/test_learning_policy_replay.py.
- [x] RED: no future evidence/correction leakage, same candidate scope for all policies, deterministic tie-breaks, empty histories explicit, revision/reset/assisted/revoked exclusions, genuinely delayed different-family retests only.
- [x] Implement per-assessment decision-time replay from frozen snapshots; separate state prediction errors, policy proxy metrics and observed delayed-retest metrics; no counterfactual outcome invention.
- [x] GREEN: targeted replay and immutable-evidence/correction integration tests.

## E. Integrate and expose
Files: learning/api/application.py, assessment state projection after A completes, API contract fixture/test, frontend/src/app.js/api.js plus a focused learning feature UI module and frontend tests; docs/API.md and implementation report.
- [x] Wire new routers and unified state scheduling. Verify schemas/compatibility with explicit additive contract fixtures.
- [x] Add the four live-data UI flows using existing styling/auth/error states; show no-data and non-causal evaluation boundaries. Test request shapes, safe rendering and answer progression.
- [x] Independent review of cross-module data, concurrency and time semantics; reproduce and fix actionable findings.
- [x] Run full backend and frontend checks and record results.

Validation command (from py): `.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider --basetemp=<unique writable path outside Git> knowpath_backend/test`. Set PYTHON_DOTENV_DISABLED=1 and PYTHONDONTWRITEBYTECODE=1; remove LEARNING_TEST_*, RAG_TEST_*, KNOWPATH_TEST_* and live service/model connection variables from the child test process. Use frontend `npm run check` and `npm test`. Never read/change real .env or run real data migrations.

## Integration checkpoint
- Final full backend run: 2653 passed / 263 skipped / 0 failed in 240.47 seconds; one pre-existing Starlette/httpx deprecation warning. The original four failures were fixed and the final run includes subsequent concurrency, legacy-cache and real-evidence regression additions.
- Frontend syntax checks and all 28 Node tests passed independently. Isolated Edge smoke exercised all four flows with 22 intercepted API requests; desktop (1280px) and mobile (390px) checks and visual inspection passed with no real backend/model calls.
- In-process API integration covers all four features against shared memory and SQLite histories, including immutable state and no fabricated same-session delayed retests. Additive OpenAPI contracts pass without replacing historical fixtures.
- Independent review reproduced and fixed completed-history cutoff, retest spacing, combined replay capacity, exact budget due time, legacy adaptive answer-time hydration, concurrent family exposure and shared read-only revalidation of cached mastery.
- Code is handed off and verification is complete. Local module commits preserve the validated implementation; no real service/model integration, migration, push or deployment was performed.
