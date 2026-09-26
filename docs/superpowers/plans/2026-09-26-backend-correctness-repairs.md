# Backend Correctness and Local Storage Repairs

**Goal:** Repair the four reviewed issues before adding new algorithm/product features.

**Architecture:** Preserve immutable assessments, evidence, and task history. Distinguish a new executable learning cycle from historical task completion. Record assessment assistance independently of natural-language hint classification. Use explicit human-confirmed question revisions and transactional evidence replacement. Restrict storage listeners and require externally supplied secrets.

**Tech stack:** Existing Python/FastAPI, SQLAlchemy/MySQL/SQLite, pytest, Docker Compose.

User authorization: implement all four repairs in the current workspace and subsequently create modular local commits. Existing unrelated changes remain intact; no deployment, secret rotation, or data-volume reset is part of this task. Independent modules are implemented concurrently under the dispatching-parallel-agents workflow, followed by root integration review.

## 1. Plan task cycles (root)
- [x] Add regressions in `py/knowpath_backend/test/test_learning_plan_cycles.py`: completed learning followed by due review; terminal task followed by new weak evidence; same review cycle must not duplicate; deferred task returns to pending at its timestamp; future deferral remains respected; historical task remains traceable; time-only due transition marks a plan stale.
- [x] Run the tests before implementation and confirm behavioral failures.
- [x] Update `learning/plans/policy.py` and narrowly `learning/plans/service.py`: persist a cycle signature, preserve historical tasks when replacing cycles, restore elapsed deferrals, and make time transitions visible without invalidating every read.
- [x] Run plan/session and knowledge-update regressions; resolve the independent review's legacy preference, invalidated deferral, and clock-only diagnostic cases (82 passed, 21 skipped).

## 2. Assistance integrity (assistance_integrity)
- [x] Reproduce normal explanation being counted as unassisted assessment evidence, including source overlap and generation/finalization boundaries.
- [x] Track source-bound assistance using assessment/topic/source/time context; keep response-style hint detection separate.
- [x] Verify memory/SQLite persistence, cancellation/retries, unrelated sources and completed observations; audit historical SSE replay, reconstruct legacy source provenance, and reuse the same fence for direct source access. Final focused integrity/source/RAG-message run: 86 passed.

## 3. Storage configuration (storage_security)
- [x] Add failing configuration/client tests.
- [x] Bind storage ports to loopback, remove shipped weak secret fallbacks, propagate configured Qdrant authentication to production clients.
- [x] Update configuration examples and migration instructions without reading or changing existing credentials/containers.
- [x] Validate Compose with disposable dummy credentials and run configuration/client regressions.

## 4. Question dispute/correction (question_correction)
- [x] Add failing tests for quarantine, explicit source-backed confirmation, immutable correction, invalidation, rollback, replay and conflicting revisions.
- [x] Add request/read/confirm review APIs and persist audit records in the assessment snapshot.
- [x] Recompute outcomes and evidence atomically; preserve original observations and do not resurrect assisted/reset/stale/quarantined evidence through legacy regrade.
- [x] Document the local human adjudication contract and update the intentional API snapshot.

## Verification
Run the existing virtualenv with `PYTHON_DOTENV_DISABLED=1`, remove `LEARNING_TEST_*`, `RAG_TEST_*`, `KNOWPATH_TEST_*` and live service/model connection settings from the test process, and disable pytest's cache provider. Focused tests can use unique workspace `.tmp` directories; the complete suite must use a unique writable directory outside the Git tree because project-root discovery tests require a non-repository ancestor. First execute focused regression modules, then the complete backend suite and frontend checks if API consumers are affected. Inspect all diffs and verify no unrelated changes, secret values, or production data mutations. Record final results and any unexecuted external-service tests.

- [x] Resolve independently reproduced review findings and full-suite regressions.
- [x] Final backend suite: 2493 passed, 251 skipped, one pre-existing Starlette/httpx deprecation warning; exit 0 in 211.68 seconds.
- [x] Frontend syntax checks and all 12 tests passed.
- [x] Independent review completed and git diff --check passed.
- [x] Document deployment/manual-adjudication limits and unexecuted live MySQL/external-service validation in docs/implementation/2026-09-26-backend-correctness-repairs.md.
