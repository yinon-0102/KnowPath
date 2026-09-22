# B2-R1 Continuation Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the evaluation-only B2-R1 continuation-unit closure plugin, correct the retrieval evaluator, build a frozen B2 comparison dataset from real sources, and run a reproducible A/B2 evaluation.

**Architecture:** Keep OrdinaryPlugin's BM25/vector/RRF retrieval unchanged. Add a deterministic continuation closure layer after A seed retrieval and before the existing reranker; it only expands complete authorized continuation units when candidate and context budgets fit, otherwise it preserves the A seed. Keep all output as authorized leaf identities and keep production answering, citation, and verification paths unchanged.

**Tech Stack:** Python 3.12, Pydantic, pytest, SQLAlchemy, Qdrant, existing frozen RAG evaluator, JSONL evaluation artifacts.

---

## Scope and safety constraints

- Work only in `C:/Users/27202/Desktop/KnowPath/.worktrees/rag-foundation`.
- Do not reset or overwrite the existing uncommitted v11 changes.
- Do not print, copy, or commit `.env` values or API keys.
- Do not change the default production plugin or add provider calls.
- Commit format must remain an English prefix followed by a Chinese note, for example `feat:新增B2-R1闭包检索`.
- Every production-code change follows a red-green-refactor loop with a failing test observed first.

### Task 1: Add failing unit tests for continuation closure

**Files:**
- Create: `py/knowpath_backend/test/test_rag_b2_r1.py`
- Reference: `py/knowpath_backend/learning/rag/plugins.py`
- Reference: `py/knowpath_backend/learning/rag/contracts.py`

- [ ] **Step 1: Add a test fixture for authorized leaf rows, a fake embedder, a fake dense backend, and a retrieval request with `rerank_candidates=5` and `context_tokens=100`.

- [ ] **Step 2: Write `test_continuation_closure_emits_complete_unit_in_ordinal_order`.
  Arrange one A seed from a two-leaf continuation unit and one unrelated A seed. Assert the B2 result contains both unit leaves in ordinal order, records `edge_type=continuation`, and keeps every candidate under the authorized identities.

- [ ] **Step 3: Write `test_continuation_closure_keeps_seed_when_complete_unit_does_not_fit`.
  Arrange a seed whose complete unit would exceed the remaining candidate or context budget. Assert the seed remains, no partial unit is emitted, and the trace records `skipped_closures` with the exact reason.

- [ ] **Step 4: Write `test_continuation_closure_deduplicates_repeated_seed_units`.
  Arrange two A seeds from one unit. Assert every unit leaf appears once and the original A order is stable for unrelated candidates.

- [ ] **Step 5: Write `test_continuation_closure_rejects_cross_version_unit`.
  Arrange two rows sharing a `continuation_of` value but having different material or retrieval versions. Assert the plugin falls back to the A result and traces `structure_unavailable`.

- [ ] **Step 6: Run the new test file and verify it fails for the missing `b2_r1` implementation.

Run from `py`:

```powershell
$env:UV_CACHE_DIR='C:\Users\27202\Desktop\KnowPath\.worktrees\rag-foundation\py\.uv-cache'
uv run --project . pytest --basetemp=.pytest-b2-red -q knowpath_backend/test/test_rag_b2_r1.py
```

Expected: collection or assertion failures because `ContinuationClosurePlugin` and the `b2_r1` registry mode do not exist.

### Task 2: Implement the pure continuation closure plugin

**Files:**
- Create: `py/knowpath_backend/learning/rag/continuation.py`
- Modify: `py/knowpath_backend/learning/rag/registry.py`
- Test: `py/knowpath_backend/test/test_rag_b2_r1.py`

- [ ] **Step 1: Implement `unit_key(row)` and `validate_units(rows)` in `continuation.py`.
  Group by `continuation_of or chunk_id`; require identical material version, retrieval version, parent, nonnegative ordinal, nonempty source text, and source spans for all members. Return deterministic ordinal-sorted unit lists or raise `RetrievalError("STRUCTURE_UNAVAILABLE")`.

- [ ] **Step 2: Implement `ContinuationClosurePlugin(OrdinaryPlugin)`.
  Call `super().retrieve` exactly once to obtain A seeds and its single embedding call. Build unit groups only from the already authorized rows. Traverse seeds in base order; emit a complete multi-leaf unit only when its candidate count and conservative source-token estimate fit the remaining `rerank_candidates` and `context_tokens` budgets. Otherwise emit the original seed. Fill unused capacity with un-emitted A candidates in original order. Never expand a unit not represented by an A seed.

- [ ] **Step 3: Emit explicit trace fields.
  Include `seeds`, `closures`, `skipped_closures`, `replacements`, `structure_version_ids`, `structural_additions`, `a_retention_at_40`, and `structure_unavailable`. Every closure member must include its unit key, seed id, target id, `edge_type=continuation`, material version, retrieval version, and tree version if present.

- [ ] **Step 4: Register `b2_r1` in `REGISTRY` and `TRACE_FIELDS`, while leaving `a`, `a_large`, `b1`, and `b15` behavior unchanged.

- [ ] **Step 5: Run the new test file and the existing plugin tests.

Expected: all B2-R1 unit tests and existing registry/plugin tests pass.

- [ ] **Step 6: Commit the isolated plugin change.

```powershell
git add py/knowpath_backend/learning/rag/continuation.py py/knowpath_backend/learning/rag/registry.py py/knowpath_backend/test/test_rag_b2_r1.py
git commit -m "feat:新增B2-R1闭包检索"
```

### Task 3: Integrate the mode without changing production defaults

**Files:**
- Modify: `py/knowpath_backend/learning/rag/pipeline.py`
- Modify: `py/knowpath_backend/learning/rag/runtime.py`
- Modify: `py/knowpath_backend/learning/rag/registry.py`
- Modify: `py/knowpath_backend/learning/rag/lifecycle.py` only if the existing tree readiness guard needs the explicit B2 mode
- Test: `py/knowpath_backend/test/test_rag_b2_r1.py`
- Test: `py/knowpath_backend/test/test_rag_runtime.py`
- Test: `py/knowpath_backend/test/test_rag_lifecycle_entrypoints.py`

- [ ] **Step 1: Add a failing integration test asserting that `b2_r1` requires a validated structural tree manifest and never changes the default `a` mode.

- [ ] **Step 2: Add `b2_r1` to the explicit mode allowlist and make it require the same validated tree readiness as B1. Keep the default environment mode as `a` and reject experimental mode only when its manifest is not structurally ready.

- [ ] **Step 3: Add `tree_version_id` to the request-local authorized row projection in `RagPipeline._originals`, sourced from the pinned manifest. Do not expose any new source text to the plugin.

- [ ] **Step 4: Ensure the pipeline still reranks exactly once and still reconstructs citations from authoritative SQL originals. Do not modify generator, verifier, or citation contracts.

- [ ] **Step 5: Run the targeted integration tests and the existing RAG lifecycle/runtime suite.

### Task 4: Correct evaluator ranking and add B2-R1 attribution metrics

**Files:**
- Modify: `py/knowpath_backend/rag_eval/scoring.py`
- Modify: `py/knowpath_backend/rag_eval/dataset.py`
- Modify: `py/knowpath_backend/rag_eval/runner.py` only if the B2 trace needs explicit mode metadata
- Test: `py/knowpath_backend/test/test_rag_eval.py`
- Test: `py/knowpath_backend/test/test_rag_b2_r1.py`

- [ ] **Step 1: Add failing scorer tests showing that NDCG ideal relevance is fixed from gold evidence, not from the number of relevant rows retrieved in the current list.

- [ ] **Step 2: Implement fixed-gold NDCG using the number of distinct gold evidence-bearing items as the ideal count for every candidate/rerank list.

- [ ] **Step 3: Add `unit_recall`, `structural_additions`, `a_retention_at_40`, `replacement_count`, `skipped_closure_count`, and negative-control expansion metrics. Keep service success and human quality fields separate.

- [ ] **Step 4: Extend the evaluator mode specification with `b2_r1` as a runtime mode; retain `parent_merge` and `typed_edge` as injected-only modes and do not silently convert them to B2.

- [ ] **Step 5: Run all evaluator tests and confirm old A/B1 reports remain schema-compatible.

- [ ] **Step 6: Commit scorer and evaluator changes.

```powershell
git add py/knowpath_backend/rag_eval/scoring.py py/knowpath_backend/rag_eval/dataset.py py/knowpath_backend/rag_eval/runner.py py/knowpath_backend/test/test_rag_eval.py py/knowpath_backend/test/test_rag_b2_r1.py
git commit -m "fix:修正B2评测指标"
```

### Task 5: Build and freeze the B2-R1 real-source dataset

**Files:**
- Create or modify: `py/.rag-evaluation/tree-b2-r1-v1/` artifacts
- Modify: `py/knowpath_backend/rag_eval/build_tree_dataset.py` only for deterministic continuation-unit labels and negative controls
- Test: `py/knowpath_backend/test/test_rag_eval.py`

- [ ] **Step 1: Add a dataset validation test requiring exact source hashes, approved review status, continuation unit ids for positive questions, and forbidden structural additions for controls.

- [ ] **Step 2: Generate the largest honest set from the three frozen real documents. Target 40 development plus 20 heldout; if the audited source inventory cannot satisfy the composition, record the smaller frozen size and do not fabricate mechanically adjacent questions.

- [ ] **Step 3: Ensure each positive question requires at least two leaves from one continuation unit, each single-leaf control has no required continuation partner, and each false-neighbor control explicitly labels adjacent distractors.

- [ ] **Step 4: Freeze the dataset, runtime bindings, manifest hashes, model profile, database identity, Qdrant prefix, and `b2_r1` mode with no API keys in artifacts.

- [ ] **Step 5: Run dataset integrity validation and the offline scorer against a synthetic trace fixture only to verify protocol arithmetic; label it as injected/offline and never use it as a quality result.

- [ ] **Step 6: Commit only the non-secret dataset/freeze artifacts.

```powershell
git add py/.rag-evaluation/tree-b2-r1-v1 py/knowpath_backend/rag_eval/build_tree_dataset.py py/knowpath_backend/test/test_rag_eval.py
git commit -m "data:冻结B2-R1真实题集"
```

### Task 6: Run the real A/B2-R1 evaluation and report final metrics

**Files:**
- Create: `py/.rag-evaluation/tree-b2-r1-v1/dev-real.jsonl`
- Create: `py/.rag-evaluation/tree-b2-r1-v1/report-real.json`
- Create: `py/.rag-evaluation/tree-b2-r1-v1/summary.md`

- [ ] **Step 1: Load the approved main-workspace `py/.env` only into the evaluation process; override database and collection prefix to the frozen isolated evaluation bindings without modifying `.env`.

- [ ] **Step 2: Run the frozen A/B2-R1 development partition with zero harness retries and write a new result file without deleting prior evidence.

- [ ] **Step 3: Verify row count, per-mode counts, duplicate keys, service failures, error codes, call counts, usage-known rate, and latency before scoring.

- [ ] **Step 4: Generate the fixed-gold report and compute A-vs-B2 deltas overall, by domain, by relation type, and by control category.

- [ ] **Step 5: If development passes every gate, run heldout with the same frozen configuration and no rule changes. If any gate fails, stop and report the failure rather than tuning B2.

- [ ] **Step 6: Write a final summary that separates retrieval metrics, context/citation coverage, service reliability, latency, and unavailable human answer-quality metrics. Do not claim production adoption unless all declared gates pass.

### Verification checklist

- [ ] `pytest` targeted B2-R1 and evaluator tests pass.
- [ ] Existing RAG lifecycle, runtime, context, verification, and evaluator tests pass.
- [ ] `git diff --check` passes.
- [ ] No API key, DSN secret, response body, or `.env` value appears in tracked files or reports.
- [ ] Real evaluation has exactly the frozen scheduled rows, with no missing or duplicate execution rows.
- [ ] Final report distinguishes service success from human-reviewed answer quality.
