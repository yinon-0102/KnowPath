# B4 Final Tree Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkboxes for tracking. User authorized implementation and one real evaluation; commits and pushes remain prohibited pending result review.

**Goal:** Implement the approved B4 bounded navigation experiment and compare 40 original questions in A0/F/B4 once (120 terminal keys).

**Architecture:** Preserve authoritative source leaves and shared answer verification. Generate version-bound chapter/packet navigation cards, compare flat selection with bounded tree traversal, then rerank and pack original text. Persist retrieval snapshots before generation and freeze every evaluation input before spending on the final run.

**Tech Stack:** Python, existing Pydantic contracts, SQL source repository, Qdrant, DashScope, pytest.

Authoritative constraints: `docs/superpowers/specs/2026-09-24-b4-final-tree-navigation-design.md`. The user's subsequent approval supersedes that document's pending-approval status. No question/gold-dependent indexing; no production collection changes; no credential output.

## Task 1: Shared verification repair

Files: `py/knowpath_backend/learning/rag/verification.py`, `schema_diagnostics.py`, `model_services.py` only when protocol binding requires it; tests `py/knowpath_backend/test/test_rag_b4_verification.py` and existing verifier suites.

- [ ] Reproduce known-ID `s11` and conceptual `二元对举` false positives; include real money, age, dimension and unknown alphanumeric counterexamples.
- [ ] Run new tests and confirm intended failures before editing implementation.
- [ ] Make numeric extraction request-ID aware and conservative for ambiguous concepts, retaining genuine quantities.
- [ ] Replace redundant check citation declarations with claim/evidence IDs, derive source subset locally, deduplicate before bounds; reject unknown/new sources and preserve semantic/qualifier/dependency/requirement checks.
- [ ] Persist sanitized pre/post validation diagnostic objects locally; clarify real dependency prompt without deleting dependency safeguards.
- [ ] Run focused existing/new tests, report exact commands/counts; review spec then quality. No commit.

## Task 2: Canonical packets and derived index

Files: new `py/knowpath_backend/learning/rag/evidence_packets.py`, `navigation_index.py`; tests `test_rag_evidence_packets.py`, `test_rag_navigation_index.py`.

- [ ] Add deterministic tests for whole continuation units, real-parent boundaries, <=8 leaves/1600 count, oversize exclusion, immutable spans, hashes, changed source rejection, scope filtering and no overlaps.
- [ ] Observe failures, implement fixed greedy packet partitions over `build_units` output; keep source metadata unchanged.
- [ ] Define cards from real section metadata and packet descendants, with canonical IDs, source hashes/maps, short cached summary, model/prompt identity and cached summary embeddings.
- [ ] Plan all source-ordered summary partitions/merges before model calls; reject >400 calls; enforce 8000/512 per call and persist usage/cache entries.
- [ ] Implement scoped hybrid card search using existing equal RRF and query vector, isolated versioned index; verify authoritative sources at load.
- [ ] Test summary cache invalidation, budget preflight, malformed output, unknown IDs, no gold inputs, scopes and ordered full-source coverage. Review spec then quality. No commit.

## Task 3: Bounded tree and flat selection

Files: new `navigation.py`, `tree_navigation.py`, `flat_navigation.py`; tests `test_rag_b4_navigation.py`.

- [ ] Test <=2 calls/20 seconds total, <=4000/512 per call, 12/24 menus, <=3 selections, invalid IDs/empty/timeouts exact A fallback with retained costs.
- [ ] Compute A once with reusable embedding, search cards without A seed gate; B4 expand real descendants, F flatten card scope and hybrid-rank authorized leaves/continuation units.
- [ ] Protect A top20; admit <=20 navigation leaves as complete groups, dependency closure and total<=40; fill from A.
- [ ] B4 emit packet atomic group identities only, F existing original units; keep all evidence SQL-authoritative. Test differences with deterministic fake selections and source scopes.
- [ ] Review spec then quality. No commit.

## Task 4: Runtime and pipeline integration

Files: `registry.py`, `runtime.py`, `pipeline.py`, `capacity.py`, `context.py`, eval `dataset.py`, `cli.py`; targeted existing/new tests.

- [ ] Test mode registration and protected scope, authoritative packet reconstruction, atomic packing (including dependency budget), one embedding and one rerank.
- [ ] Register experimental `a0`, `f`, `b4` without changing default; inject scoped immutable navigation index and request-local bounded navigator.
- [ ] Extend rerank validation by rebuilding canonical packets from authorized full leaves; pass local atomic group metadata to capacity without overwriting actual continuation/requires.
- [ ] Persist independent candidate/rerank/context/source-map snapshots before verifier call, including errors, and structured sanitized diagnostics.
- [ ] Run affected pipeline/runtime/capacity/registry tests and full RAG suite. Review spec then quality.

## Task 5: Frozen runner and analysis

Files: `py/scripts/run_b4_final_eval.py`, `py/knowpath_backend/rag_eval/final_tree_analysis.py`, runner support and new tests.

- [ ] Test 120 rotating keys, append-only attempts, interrupted-prefix integrity, terminal keys never resent, in-flight unknown never replayed, snapshots surviving generation failure.
- [ ] Copy original 40 question/source artifacts and read-only DB backup into isolated `py/.rag-evaluation/tree-b4-final-40-v1`; use main env privately.
- [ ] Implement prepare/index/freeze/run/report stages, bind all source/config/code/prompt/index/schedule/metric hashes, record index and online tokens/calls separately.
- [ ] Compute full Recall@1/3/5/10/20/40, NDCG10/MRR, context and citation coverage, delivery/failure categories, costs and latency, paired family bootstrap 10000 seed20260924 and identical/different-context attribution.
- [ ] Apply every design gate against both baselines, report unknown quality review honestly; test metric arithmetic with synthetic cases.

## Task 6: Final verification and one real evaluation

- [ ] Run local regression and review, verify cards/cost preflight, build isolated index within approved budget; do not proceed if prerequisites fail.
- [ ] Freeze complete experimental state; execute 120 requests once with persistent journal/logs and separate monitor.
- [ ] Verify each terminal key, no selective retry/history merge, inspect all score changes/new errors against sources and mark automated/source review separately from human review.
- [ ] Produce Chinese metric comparison and retain/drop/unknown recommendation, explicitly state single-run seen-set limits. Leave code uncommitted for user decision.

## Verification commands

Run from `py` using `.venv/Scripts/python.exe -m pytest knowpath_backend/test/test_rag_b4_verification.py -q` and each new task's named test files. Integration command: `.venv/Scripts/python.exe -m pytest knowpath_backend/test -k rag -q`. Real entrypoint will expose `prepare`, `index`, `freeze`, `run`, `report`; tests must verify stages before actual calls. Use elevated execution only if the existing venv trampoline requires it; never print env values.

## Progress

- [x] Approved design and existing dirty worktree inspected; no running Python evaluator detected.
- [x] Shared fixes (numeric/citation contract/diagnostic tests and independent review)
- [x] Index and navigation implementation (real index build remains incomplete)
- [x] Integration and scoped review (925 passing RAG tests; physical focused retrieval deadline fix verified)
- [ ] Frozen real120 and final report

### Execution checkpoint — 2026-09-24

Implementation and scoped reviews are complete. Full regression: 925 passed, 8 skipped, 1586 deselected; `pytest-post-review.xml` and `validation.json` bind all 204 implementation/test files. A fresh continuation check passed 34 index/launcher/report tests. Final prerequisite and navigation reviews are recorded and `review-attestation.json` binds the exact current source. No commits or pushes.

The original unknown summary attempt 5 remains in `initial-index-stop.json`; its specifically authorized resend succeeded. The index subsequently reached 45 completed summaries; attempt 46 failed with persisted `connect_timeout` before HTTP body transmission. Dependency/transport inspection confirmed zero retries and TCP/TLS establishment preceding HTTP request transmission. Its original cache and 21.058-second failed connection are preserved in `navigation-cache-before-connect-recovery-01.json` and `index-connection-recovery-01.json`; only that unsent call was resumed, and succeeded. Successful cache entries were preserved. An independent recovery review is being recorded. This is offline index recovery, not a rerun of any evaluation question.

Current index build is active. Completion requires 220 unique summary results and 198 card embeddings, source validation, complete index/recovery accounting, immutable freeze, exactly one 40-question x3-mode = 120-request run, source review and final comparisons. No formal evaluation requests have yet been sent. Preserve the original unknown billing attempt, distinguish pre-send connection failures from model calls, and include all recovery evidence in the final freeze. Never replay unknown paid calls or selectively rerun evaluation failures.

### Runtime recovery checkpoint — 2026-09-24

Index completed: 220 unique successful summaries, 20 embedding batches /198 cards, source validation passed. Original unknown summary attempt and two pre-send connection failures remain accounted separately. Final freeze add6084b8150d2ddea6bf3bd666b7da63065864d236119db8bbefe26bd1e44d6 binds 1062 files including recovery evidence.

Initial evaluator encountered local Qdrant down (Docker engine not running): 9 terminal VECTOR_UNAVAILABLE records and 1 pending attempt when stopped. All preserved. After user manually started Docker, all970 leaf payloads/vectors verified against original frozen fingerprint. `infrastructure-recovery.json` records that verification. Resume marks pending request unknown and executes only remaining110 requests; no replay or history merge. Formal comparison necessarily has infrastructure/missing-observation limitations. At checkpoint14/120 terminal/unknown records present,4successful since recovery. Live evaluator running; no code changes after freeze and no commits/push.
