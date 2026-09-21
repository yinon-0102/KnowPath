# Tree RAG Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Execute this plan task-by-task with verification checkpoints.

**Goal:** Build and run a 40-question real-material evaluation focused on tree-based adjacent-chunk recovery, then report paired A/B1 retrieval and service metrics.

**Architecture:** Reuse the frozen v10 source artifacts and live runtime bindings. Generate a new tree-v1 dataset with exact source-span gold labels, extend the offline scorer with ranking and tree-attribution metrics, freeze the dataset, run A and B1 against the same configured services, and score the append-only result file.

**Tech Stack:** Python 3.13, uv, pytest, SQLite evaluation bootstrap, existing DashScope-compatible model/embedding/reranker adapters, Qdrant, and the `knowpath_backend.rag_eval` CLI.

---

### Task 1: Generate the tree-focused dataset

**Files:**
- Create: `py/knowpath_backend/rag_eval/build_tree_dataset.py`
- Create: `py/.rag-evaluation/tree-v1/dataset.jsonl`
- Create: `py/.rag-evaluation/tree-v1/split.json`
- Create: `py/.rag-evaluation/tree-v1/rubric.md`
- Create: `py/.rag-evaluation/tree-v1/development.json`

- [ ] Read v10 dataset labels and the evaluation SQLite chunk payloads.
- [ ] Select 40 same-parent ordinal-adjacent evidence pairs from the three frozen materials, rejecting pairs without exact source-span metadata.
- [ ] Write four ten-question strata with two necessary evidence spans and combined required answer points per question.
- [ ] Validate every quote against the frozen source artifact and assert exactly 40 unique IDs, four strata of ten, and at least two necessary spans per item.

### Task 2: Add ranking and tree attribution to offline scoring

**Files:**
- Modify: `py/knowpath_backend/rag_eval/scoring.py`
- Test: `py/knowpath_backend/test/test_rag_eval.py`

- [ ] Add exact-span hit detection over `candidate_sources` and `reranked_sources`.
- [ ] Aggregate Recall@1/3/5/10/20, MRR, NDCG, extension-only recall, extension precision, candidate/context differences, and per-stratum values.
- [ ] Add regression tests for one-hit, multi-gold, no-hit, extension-only, and empty-gold cases.
- [ ] Run the focused scorer tests before freezing the new dataset.

### Task 3: Freeze the paired experiment

**Files:**
- Create: `py/.rag-evaluation/tree-v1/freeze.json`

- [ ] Build the freeze configuration from the current `.env`-resolved runtime without writing any credentials.
- [ ] Verify source hashes, runtime manifest bindings, profile hashes, and the 40-question dev split.
- [ ] Record a new freeze ID and confirm the heldout partition is empty and never used for adoption claims.

### Task 4: Run A/B1 on the real services

**Files:**
- Create: `py/.rag-evaluation/tree-v1/dev.jsonl`
- Create: `py/.rag-evaluation/tree-v1/setup.json`

- [ ] Bootstrap or reuse the exact three-material ordinary/tree indexes required by the new freeze.
- [ ] Execute all 40 questions in both modes with alternating order and zero harness retries.
- [ ] Confirm 80 scheduled rows, no missing rows, and record provider/Qdrant/database failures without masking them.

### Task 5: Score and verify the report

**Files:**
- Create: `py/.rag-evaluation/tree-v1/report.json`
- Create: `py/.rag-evaluation/tree-v1/summary.md`

- [ ] Run the scorer against the frozen results and write the report.
- [ ] Independently recompute row counts and the main ranking metrics from `dev.jsonl`.
- [ ] Report A/B1 deltas, extension-only gains, latency, service failures, and the fact that human answer quality remains unscored.
- [ ] Run `git diff --check`, focused tests, and final status checks; commit implementation changes with an English prefix and Chinese note.
