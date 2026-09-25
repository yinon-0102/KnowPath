# B3 Unit-First Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and evaluate B3 as a unit-first structural retrieval strategy that converts continuation evidence into final context and citation coverage without changing the production publication or introducing tunable weighted scores.

**Architecture:** Reuse the B2-R1 leaf index, source authorization, manifest lifecycle, trace schema, and frozen evaluator, but add a separately versioned semantic-unit index. A semantic unit is the deterministic ordered set of parser-derived leaves sharing one continuation identity. B3 reuses one query embedding to retrieve leaves and units, admits complete units through explicit rank rules, sends unit representations through one rerank request, then expands selected units atomically into authorized leaves for context packing. A remains the hard fallback; parent nodes and derived summaries are never sent to generation in this plan.

**Tech Stack:** Python 3.13, SQLAlchemy/SQLite, Qdrant, existing DashScope-compatible embedding/rerank/chat services, pytest, frozen JSONL evaluator, existing manifest and scope authorization contracts.

---

## Decision record

B3 is a new online decision layer built on the validated B2 foundation. It is not a parameter-tuning pass over `ContinuationClosurePlugin`, and it does not replace the parser or rebuild the project around RAPTOR-style recursive summaries.

B2 demonstrated a real candidate-layer signal: candidate Recall@3 improved from 0.90 to 1.00 and candidate NDCG@10 improved from 0.9229 to 0.9765. The signal disappeared after reranking and context packing because B2 added leaf IDs before reranking while the final context selector still treated each leaf independently. B3 therefore makes the semantic unit the common object for retrieval admission, reranking, and context selection.

The initial B3 experiment is retrieval-only. Unit representations are deterministic concatenations of authorized leaf text with a source map; no generated summary is used. A later summary experiment, if needed, must be a separate mode and separate freeze.

## Fixed invariants

- Production manifests, production Qdrant collections, and the main `.env` are unchanged.
- The B2-R1 v2 source versions, 640-byte leaf chunk profile, 10-question dataset, and pending human-review status remain the comparison baseline.
- Every final candidate is an authorized leaf. Unit and parent records are navigation/rerank records only and cannot become citations.
- Each request performs one query embedding and at most one rerank request.
- The final leaf candidate count remains at most 40 and the context token budget remains the frozen 5,000-token budget.
- Unit admission uses explicit rank and provenance rules. No new weighted sum, learned score, or hand-tuned coefficient is introduced.
- If any B3 structural validation or unit admission check fails, the request returns the ordinary A candidate set and records a typed fallback reason.

## Predeclared B3 gates

B3 is eligible for further human review only when all of the following hold on the frozen development set:

1. Continuation-positive full-unit Recall@10 improves by at least 3 percentage points over A, and final context evidence coverage improves by at least 3 percentage points.
2. Citation evidence coverage on continuation positives improves by at least 3 percentage points; a candidate-only improvement is insufficient.
3. Full-set leaf NDCG@10 decreases by no more than 1 percentage point.
4. Single-leaf and adjacency negative controls have zero structural expansion and unchanged context/citation coverage.
5. A retention@40 is at least 0.98, P95 latency is at most 1.2 times A, provider failures are zero, and every mode has equal embedding/rerank call counts.
6. Human-reviewed answer correctness and citation support are approved before any end-to-end adoption claim. With `human_review_status: pending`, the report must say answer quality is unknown.

Failure of any gate means the default remains A. The experiment must report which gate failed; it must not tune a threshold and rerun until the gate passes.

### Task 1: Freeze the B3 contract and unit identity

**Files:**
- Create: `py/knowpath_backend/learning/rag/units.py`
- Modify: `py/knowpath_backend/learning/rag/contracts.py`
- Modify: `py/knowpath_backend/learning/rag/registry.py`
- Test: `py/knowpath_backend/test/test_rag_b3.py`

- [ ] **Step 1: Write failing unit identity and source-map tests.**

Add tests that construct authorized leaf rows with a root and continuation leaves and assert that:

```python
unit = build_unit([root, continuation], tree_version_id="tree-v1")
assert unit.unit_id == build_unit([continuation, root], tree_version_id="tree-v1").unit_id
assert unit.leaf_ids == (root["chunk_id"], continuation["chunk_id"])
assert unit.source_spans == tuple(root["source_spans"] + continuation["source_spans"])
```

Also assert that mixed material versions, retrieval versions, parent IDs, or tree versions raise `STRUCTURE_UNAVAILABLE`, and that a unit record cannot be converted into a `Candidate` citation identity.

- [ ] **Step 2: Define the frozen unit contracts.**

Add immutable contracts for a unit record and a rerank group. The records must include `unit_id`, `retrieval_version_id`, `material_version_id`, `tree_version_id`, `parent_id`, ordered `leaf_ids`, ordered `source_spans`, `retrieval_text`, `token_count`, and a deterministic `source_map_hash`. Extend the retrieval result contract with an optional request-local rerank plan rather than passing untrusted unit text through the existing leaf candidate list.

- [ ] **Step 3: Implement deterministic unit construction.**

In `units.py`, group only already-authorized leaves by `continuation_of or chunk_id`, sort by `(ordinal, chunk_id)`, validate one version/parent/tree identity, concatenate the original leaf text with a fixed separator, and compute the source map from the original spans. The canonical unit identity must hash the retrieval version, material version, tree version, and ordered leaf IDs. Do not infer relations from model output or adjacent section names.

- [ ] **Step 4: Run the focused contract tests.**

Run:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest -q knowpath_backend/test/test_rag_b3.py
```

Expected result after implementation: all unit identity, ordering, provenance, and rejection tests pass.

### Task 2: Build and validate the isolated semantic-unit index

**Files:**
- Modify: `py/knowpath_backend/learning/rag/building.py`
- Modify: `py/knowpath_backend/learning/rag/vector.py`
- Modify: `py/knowpath_backend/learning/rag/lifecycle.py`
- Modify: `py/knowpath_backend/learning/rag/verification.py`
- Modify: `py/knowpath_backend/learning/rag/cli.py`
- Test: `py/knowpath_backend/test/test_rag_lifecycle_entrypoints.py`
- Test: `py/knowpath_backend/test/test_rag_verification_v2.py`

- [ ] **Step 1: Add a versioned unit-index build profile.**

Extend the isolated builder with a `unit_profile` that records `unit_schema_version`, separator revision, source-map revision, embedding provider/model/dimension, unit collection name, and the leaf manifest IDs used to derive it. The profile must be part of the manifest configuration hash and must never overwrite a leaf collection.

- [ ] **Step 2: Create unit vectors from deterministic representations.**

Build unit records from the authorized leaf manifest, embed their deterministic `retrieval_text` offline, and write them to a separate Qdrant collection with payload fields for unit ID, leaf IDs, retrieval version, material version, tree version, source-map hash, and embedding profile. Reuse the existing point identity and collection verification rules; reject a payload whose source-map or profile does not match the manifest.

- [ ] **Step 3: Add lifecycle checks.**

Require B3 runtime validation to pin both the ordinary leaf manifest and the matching unit manifest. Verify that every unit leaf exists in the pinned leaf manifest, all source spans are within the scope snapshot, and the unit collection has the expected dimension, distance, profile, and point count. A missing or mismatched unit index must produce a typed B3 fallback, never a partial unit.

- [ ] **Step 4: Build only the isolated B3 index.**

Copy `py/.rag-evaluation/tree-b2-r1-v2/evaluation.db` to `py/.rag-evaluation/tree-b3-unit-v1/evaluation.db`, set the process-local collection prefix to `knowpath_rag_b3_unit_v1`, and rebuild the three unit indexes from the existing READY 640-byte leaf manifests. Export per-material unit manifests containing unit IDs, leaf ordinals, source-map hashes, and collection/profile identities. Do not touch the production database, publication, or `.env`.

- [ ] **Step 5: Audit the isolated index before model calls.**

Assert for each material that every unit has at least one leaf, multi-leaf units have contiguous ordinals, source-map hashes reconstruct the exact leaf text, no unit crosses a material/retrieval/tree version, and the number of unit vectors equals the unit manifest count. Record these facts in `py/.rag-evaluation/tree-b3-unit-v1/precondition.json` without credentials.

### Task 3: Implement B3 unit-first retrieval and atomic rerank/context selection

**Files:**
- Create: `py/knowpath_backend/learning/rag/unit_first.py`
- Modify: `py/knowpath_backend/learning/rag/plugins.py`
- Modify: `py/knowpath_backend/learning/rag/registry.py`
- Modify: `py/knowpath_backend/learning/rag/pipeline.py`
- Modify: `py/knowpath_backend/learning/rag/context.py`
- Modify: `py/knowpath_backend/learning/rag/runtime.py`
- Test: `py/knowpath_backend/test/test_rag_b3.py`
- Test: `py/knowpath_backend/test/test_rag_context.py`
- Test: `py/knowpath_backend/test/test_rag_runtime.py`

- [ ] **Step 1: Write failing retrieval-plan tests.**

Cover these cases:

```python
result = b3.retrieve(request, leaves, units)
assert result.trace["embedding_calls"] == 1
assert result.trace["rerank_mode"] == "unit_atomic"
assert all(candidate["chunk_id"] in authorized_leaf_ids for candidate in result.candidates)
assert result.trace["unit_expansions"] == expected_units
```

Add tests for unit hits that have no leaf seed, leaf seeds with no unit hit, mixed-version units, duplicate leaves, candidate budget overflow, context budget overflow, and deadline expiry. The expected fallback is A with a typed reason for every rejection.

- [ ] **Step 2: Reuse one query vector for leaf and unit retrieval.**

Implement B3 so the first hybrid leaf retrieval creates the query vector, then the unit search reuses that vector. The unit search must not call the embedder again. Keep the leaf and unit rankings in the trace with their exact manifest and collection identities.

- [ ] **Step 3: Apply explicit unit admission rules.**

Admit a unit only when it is returned by the pinned unit index and has at least one authorized leaf seed in the leaf retrieval scope. Deduplicate by unit ID, reject cross-version members, and retain the A leaf seed. Order admitted units by the best member rank, then by whether multiple independently retrieved leaves support the unit, then by document ordinal and unit ID. Do not compute a weighted score.

- [ ] **Step 4: Rerank unit representations once.**

Pass at most 40 unit/leaf rerank groups to the existing reranker in one request. Each rerank group must use the deterministic unit representation and carry a source map back to leaf IDs. The reranker may order groups only; it may not add IDs or source spans. Record the exact rerank group order and score mapping in the trace.

- [ ] **Step 5: Expand selected groups atomically.**

After reranking, expand selected groups back to authorized leaves in ordinal order. Count the final leaf list against the 40-candidate budget. During context packing, admit or skip a whole group according to the existing 5,000-token budget, while keeping the A seed when a complete group cannot fit. The final `context_ids`, citation IDs, and evidence checks must contain leaves only.

- [ ] **Step 6: Add safe fallback and trace fields.**

Register `b3_unit` as a separate runtime mode. Add trace fields for unit hits, admitted/rejected units, rerank groups, atomic expansions, skipped groups, A retention, fallback reason, embedding call count, rerank call count, and unit manifest identities. Any malformed unit plan or unit-index mismatch must return A candidates with `fallback_reason`, never silently behave as B2.

- [ ] **Step 7: Run the B3 focused regression suite.**

Run:

```powershell
$testTmp = Join-Path (Get-Location) '.pytest-tmp'
$env:TEMP=$testTmp; $env:TMP=$testTmp; $env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest -q --basetemp (Join-Path $testTmp 'b3') `
  knowpath_backend/test/test_rag_b3.py `
  knowpath_backend/test/test_rag_context.py `
  knowpath_backend/test/test_rag_runtime.py
```

Expected result: B3 unit admission, one-embedding accounting, atomic context behavior, authorization, and fallback tests pass while A and B2 tests remain unchanged.

### Task 4: Extend the frozen evaluator for B3 attribution

**Files:**
- Modify: `py/knowpath_backend/rag_eval/dataset.py`
- Modify: `py/knowpath_backend/rag_eval/runner.py`
- Modify: `py/knowpath_backend/rag_eval/scoring.py`
- Modify: `py/knowpath_backend/rag_eval/cli.py`
- Test: `py/knowpath_backend/test/test_rag_eval.py`

- [ ] **Step 1: Add the `b3_unit` mode specification.**

Mark B3 as a runtime mode with the fixed leaf/context/rerank budgets and require a unit manifest binding. Preserve `a`, `b2_r1`, and all existing offline ablation semantics. Reject a freeze that enables B3 without unit manifest configuration hashes.

- [ ] **Step 2: Add unit-aware scoring.**

Score candidate and reranked leaf Recall@1/3/5/10/20/40, NDCG@1/3/5/10/20/40, and MRR using the existing fixed coordinate gold. Add full-unit Recall, complete-unit Recall, final context evidence coverage, citation evidence coverage, unit admission precision, atomic expansion count, negative-control expansion, skipped groups, A retention, and fallback counts. Never convert these retrieval metrics into answer-quality scores.

- [ ] **Step 3: Add execution integrity checks.**

Require unique `(question_id, plugin, repeat)` keys, zero missing rows, zero service failures, one embedding per non-clarify request, at most one rerank call, complete leaf/unit manifest trace identities, and identical A/B2/B3 request counts. Fail report generation when any structural identity is missing.

- [ ] **Step 4: Add evaluator tests for gate accounting.**

Test that a B3 candidate gain with unchanged context coverage does not pass, that one negative-control expansion fails the gate, that missing human review keeps answer quality unknown, and that failed or missing rows remain in the denominator.

### Task 5: Freeze the B3 development set and run the paired benchmark

**Files:**
- Create: `py/.rag-evaluation/tree-b3-unit-v1/dataset.jsonl`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/split.json`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/development.json`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/freeze.json`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/rubric.md`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/dev.jsonl`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/report.json`
- Create: `py/.rag-evaluation/tree-b3-unit-v1/summary.md`

- [ ] **Step 1: Reuse the exact B2-R1 v2 question and source bindings.**

Copy the 5 continuation positives and 5 negative controls without changing questions, gold spans, relation labels, or human-review status. Bind each row to the B3 leaf and unit manifests and record the unit source-map hash. Do not add adjacent questions merely to increase the sample size.

- [ ] **Step 2: Freeze the three-mode runtime configuration.**

Set `modes=["a", "b2_r1", "b3_unit"]`, `repeats=1`, `retries=0`, the isolated SQLite identity, the isolated Qdrant prefix, exact leaf/unit manifest configuration hashes, and `decision_rule="exploratory_no_adoption"`. Run `freeze` and `verify_freeze` before any provider call.

- [ ] **Step 3: Run A/B2/B3 with append-only results.**

Load the main workspace `.env` only inside the process, override the isolated database and collection prefix, run the development partition with zero harness retries, and preserve the raw JSONL. Expected row count is `question_count * 3`.

- [ ] **Step 4: Verify integrity before scoring.**

Check duplicate keys, missing rows, service failures, per-mode call counts, scope snapshots, leaf manifest IDs, unit manifest IDs, source-map hashes, and final candidate limits. Stop scoring if any check fails.

- [ ] **Step 5: Generate the report and apply gates once.**

Generate `report.json` and `summary.md` with the predeclared gates. Report A, B2, and B3 separately; do not retune B3 based on the first result. If B3 fails any gate, state that A remains the default and preserve the failed report for diagnosis.

### Task 6: Complete verification and handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-09-23-b3-unit-first-retrieval.md` only to check completed steps and record actual commands/results.
- No production publication changes.

- [ ] **Step 1: Run focused and complete RAG tests.**

```powershell
.\.venv\Scripts\python.exe -m pytest -q knowpath_backend/test/test_rag_b3.py knowpath_backend/test/test_rag_eval.py
.\.venv\Scripts\python.exe -m pytest -q knowpath_backend/test/test_rag_*.py
```

Use a workspace-local `TEMP`/`TMP` directory when the default Windows temp directory is inaccessible, and explicitly expand the test file list in PowerShell.

- [ ] **Step 2: Run artifact checks.**

Run `git diff --check`, verify all frozen file hashes, scan generated text artifacts for credentials, and independently count raw JSONL rows and unique execution keys.

- [ ] **Step 3: Decide adoption without committing automatically.**

If every B3 gate passes and human review is approved, present the report for user review before any commit or push. Otherwise keep A as the default, preserve the isolated B3 artifacts, and do not create a production publication. No B4 tuning cycle is part of this plan.

## Expected deliverables

- A versioned B3 unit contract and isolated unit index with source-map provenance.
- A `b3_unit` runtime mode that uses one embedding, one rerank, at most 40 leaves, and atomic unit context selection.
- Focused B3 regression tests and evaluator integrity tests.
- A frozen A/B2/B3 report with candidate, reranked, context, citation, structure, latency, reliability, and call-count metrics.
- A clear adoption decision based on the gates above, with answer quality explicitly unknown until human review is approved.
