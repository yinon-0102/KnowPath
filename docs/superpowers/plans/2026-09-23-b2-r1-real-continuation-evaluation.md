# B2-R1 Real Continuation Evaluation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated index that actually contains parser-derived continuation units, freeze a matching evaluation subset, and compare A with B2-R1 under identical retrieval and generation conditions.

**Architecture:** Copy the existing reviewed-material SQLite database into an isolated evaluation database. Rebuild each of the three material indexes with a preflight-selected 640-byte semantic chunk budget so long source units produce multiple leaves linked by `continuation_of` without splitting atomic math or code; publish B1 structural manifests without changing the production publication. Select only questions whose frozen necessary evidence spans map to at least two leaves in one continuation unit, retain single-leaf and adjacency controls, and run the existing paired A/B2 evaluator against the new bindings.

**Tech Stack:** Python 3.13, SQLAlchemy/SQLite, Qdrant, existing DashScope-compatible embedding/rerank/chat services, pytest, frozen JSONL evaluator.

---

### Task 1: Verify the source and index preconditions

**Files:**
- Read: `py/.rag-evaluation/v10/evaluation.db`
- Read: `py/.rag-evaluation/tree-reviewed-v1/dataset.jsonl`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/precondition.json`

- [ ] **Step 1: Copy the v10 SQLite database to a new isolated database.**

```powershell
Copy-Item py/.rag-evaluation/v10/evaluation.db py/.rag-evaluation/tree-b2-r1-v2/evaluation.db
```

- [ ] **Step 2: Inspect material bindings and source spans without calling providers.**

```powershell
sqlite3 py/.rag-evaluation/tree-b2-r1-v2/evaluation.db "select count(*) from learning_spaces;"
```

Record only material, version, space, and scope identities in `precondition.json`; never record credentials.

- [ ] **Step 3: Validate that the current index has no continuation units.**

```powershell
python -c "import sqlite3,json; c=sqlite3.connect('py/.rag-evaluation/v10/evaluation.db'); rows=[json.loads(x[0]) for x in c.execute('select payload from rag_chunks')]; print(sum(bool(r.get('continuation_of')) for r in rows))"
```

Expected result: zero, establishing why the previous B2 run was non-discriminating.

Offline preflight result: `max_tokens=128` and `512` reject the linear-algebra source because an atomic math/code unit exceeds the budget. `640` succeeds for all three exact indexed material versions and yields 15 law, 4 linear-algebra, and 13 Lunyu continuation groups. This value is frozen before any provider call.

### Task 2: Build isolated continuation-aware indexes

**Files:**
- Create: `py/.rag-evaluation/tree-b2-r1-v2/build/<material>.json`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/manifests/<material>.json`
- Modify only the isolated database/Qdrant collection; do not modify the production `.env`.

- [ ] **Step 1: Set process-local evaluation variables.**

```powershell
$env:DATABASE_URL = "sqlite:///C:/Users/27202/Desktop/KnowPath/.worktrees/rag-foundation/py/.rag-evaluation/tree-b2-r1-v2/evaluation.db"
$env:RAG_COLLECTION_PREFIX = "knowpath_rag_b2r1_v2"
```

- [ ] **Step 2: Build each material with the preflight-frozen `max_tokens=640` and the existing B1 builder path.**

Use `knowpath_backend.learning.rag.cli build` with `plugin: "b1"`, the existing space/material identities, and `max_tokens: 640`. Load the main workspace `.env` only inside the process. Each successful build must produce a READY ordinary manifest followed by a READY structural manifest.

- [ ] **Step 3: Inspect the generated SQL payloads.**

For every material, assert at least one `continuation_of` group has two or more leaves, all leaves share material version, retrieval version, parent, and tree version, and every source span remains authorized. Export a per-document structure manifest containing leaf IDs, ordinals, continuation IDs, parent IDs, and source spans; include its hash in the frozen runtime bindings.

- [ ] **Step 4: Validate and publish only the isolated structural manifests.**

Run the existing `validate` and `publish` commands with the expected generation from the copied database. Record manifest IDs, retrieval version IDs, tree version IDs, and configuration hashes in the isolated binding file.

### Task 3: Freeze a valid B2 development dataset

**Files:**
- Create: `py/.rag-evaluation/tree-b2-r1-v2/dataset.jsonl`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/split.json`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/development.json`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/rubric.md`

- [ ] **Step 1: Map every existing necessary-evidence span to generated leaves.**

For each question, resolve exact `(material_version_id, artifact_hash, block, start, end)` coordinates against generated chunk spans. A single required evidence interval may be covered by several leaves: mark a question as a continuation positive only when the interval's coverage uses at least two distinct leaves sharing one `continuation_of` value. Split the frozen gold interval into the exact low-index leaf intervals so ranking and coverage score the fixed union rather than an impossible single-chunk span.

- [ ] **Step 2: Select controls without inventing labels.**

Retain existing single-leaf questions as negative controls only when one leaf covers all necessary evidence and no continuation partner is required. Do not call `same_section` questions continuation positives merely because their chunks are adjacent. If fewer than 40 valid questions remain, freeze the smaller honest set. The preflight found five directly reusable continuation positives; do not inflate this count by relabeling same-section siblings or by treating a long single-leaf gold span as a semantic continuation question.

- [ ] **Step 3: Add explicit structural metadata.**

Each selected row must include `relation_type`, `relation_required`, `continuation_unit_id` for positives, `forbidden_structural_additions` for controls, exact source hashes, and `human_review_status: pending` unless a human reviewer has approved it.

- [ ] **Step 4: Freeze the runtime bindings.**

Set modes to `["a", "b2_r1"]`, bind every question to the isolated space/scope/manifest/retrieval identities, include nonsecret database identity and Qdrant prefix, and run `freeze` followed by `verify_freeze`.

### Task 4: Run the paired A/B2 evaluation

**Files:**
- Create: `py/.rag-evaluation/tree-b2-r1-v2/dev-real.jsonl`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/report.json`
- Create: `py/.rag-evaluation/tree-b2-r1-v2/summary.md`

- [ ] **Step 1: Run the frozen development partition with zero harness retries.**

Use the process-local main `.env`, override only the isolated `DATABASE_URL` and `RAG_COLLECTION_PREFIX`, and preserve all prior results. Expected rows: `selected_questions * 2`.

- [ ] **Step 2: Validate execution integrity before scoring.**

Require zero duplicate keys, zero missing rows, zero service failures, equal embedding/rerank call counts per mode, and complete scope/manifest trace identities.

- [ ] **Step 3: Generate the fixed-gold report.**

Report Recall@1/3/5/10/20/40, fixed-gold NDCG@10, MRR, continuation-unit complete recall, necessary-evidence hit rate for structural additions, A retention, replacements, skipped closures, control expansion rates, context/citation coverage, latency, service success, and provider call counts.

- [ ] **Step 4: Apply the predeclared gates without tuning.**

B2 can proceed only if continuation-positive Recall@10 improves by at least 3 percentage points, full-set NDCG@10 drops by no more than 1 point, both control expansion rates are zero, P95 is at most 1.2x A, and service failures are zero. Pending human review keeps answer quality unknown regardless of retrieval results.

### Task 5: Verify and document the result

- [ ] **Step 1: Run targeted B2/evaluator tests and the full RAG test suite.**

```powershell
uv run --python 3.13 python -m pytest -q knowpath_backend/test/test_rag_eval.py knowpath_backend/test/test_rag_b2_r1.py
uv run --python 3.13 python -m pytest -q knowpath_backend/test/test_rag_*.py
```

- [ ] **Step 2: Run `git diff --check` and scan generated artifacts for secrets.**

- [ ] **Step 3: Report whether the experiment is discriminating.**

If no valid continuation positives remain after exact span mapping, stop and report that the source/index combination cannot validate B2-R1; do not manufacture adjacent questions or claim a tree improvement.
