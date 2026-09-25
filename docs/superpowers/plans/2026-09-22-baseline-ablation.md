# A/B1 基线与树形消融评测实施计划

> **For agentic workers:** Execute this plan task-by-task with verification checkpoints.

**Goal:** Freeze A/B1 as the exploratory v1.5 baseline, rebuild a reviewed tree-focused real-material evaluation set, and run A, B1, parent_merge, and typed_edge ablations without adopting B2.

**Architecture:** Preserve the existing v10/v15 artifacts and append a new baseline/ablation directory. Treat document layout as navigation only; gold evidence is explicitly reviewed and may include single-leaf controls, parent context, or grounded typed relations. Extend the evaluation configuration and report so every mode is scored on retrieval, evidence coverage, service reliability, latency, and usage observability.

**Tech Stack:** Python 3.13, uv, pytest, existing `knowpath_backend.rag_eval` freeze/run/report CLI, SQLite/Qdrant runtime bindings, JSONL evaluation artifacts.

---

### Task 1: Freeze the exploratory A/B1 baseline

**Files:**
- Create: `py/.rag-evaluation/tree-baseline-v11/development.json`
- Create: `py/.rag-evaluation/tree-baseline-v11/freeze.json`
- Create: `py/.rag-evaluation/tree-baseline-v11/README.md`
- Modify only if required: `py/knowpath_backend/rag_eval/dataset.py`, `cli.py`

- [ ] Use the reviewed dataset and runtime configuration resolved from the local .env without writing credentials.
- [ ] Set `modes=["a","b1"]`, `decision_rule="exploratory_no_adoption"`, `repeats=1`, and `retries=0`.
- [ ] Preserve B1.5 code and old results, but exclude `b15` from the default matrix and fail validation if it is requested through this baseline.
- [ ] Record redacted model, Qdrant collection, database identity, manifest hashes, code hash, budget, and source hashes.

### Task 2: Rebuild and validate the reviewed tree evaluation set

**Files:**
- Modify: `py/knowpath_backend/rag_eval/build_tree_dataset.py`
- Create: `py/.rag-evaluation/tree-reviewed-v1/dataset.jsonl`
- Create: `py/.rag-evaluation/tree-reviewed-v1/split.json`
- Create: `py/.rag-evaluation/tree-reviewed-v1/rubric.md`
- Create: `py/.rag-evaluation/tree-reviewed-v1/review-log.jsonl`

- [ ] Keep only answerable or explicitly unanswerable/clarify items supported by the three real materials.
- [ ] Label `relation_type` as `single_leaf`, `same_section`, `parent_context`, `typed_cross_reference`, `prerequisite`, or `no_relation`.
- [ ] Store exact necessary evidence spans, required answer points, source domain, reviewer status, and reviewer rationale.
- [ ] Remove mechanically added sibling gold; preserve a single-leaf negative control where extra tree expansion should not help.
- [ ] Validate quotes, hashes, unique IDs, domain/relation coverage, answerability, and that only reviewed rows enter the dev split.

### Task 3: Prepare four-mode ablation support

**Files:**
- Modify: `py/knowpath_backend/rag_eval/dataset.py`, `runner.py`, `scoring.py`
- Modify: `py/knowpath_backend/learning/rag/registry.py`, `plugins.py` only if mode adapters are missing
- Test: `py/knowpath_backend/test/test_rag_eval.py` and focused RAG plugin tests

- [ ] Add explicit `parent_merge` and `typed_edge` evaluation modes with deterministic fallback to A when their evidence relation is absent.
- [ ] Ensure parent_merge merges parent context without adding sibling candidates; typed_edge follows only provenance-bearing typed relations.
- [ ] Report Recall@1/3/5/10/20/40, NDCG@10, candidate/context evidence coverage, extension precision and extension-only recall, service failure rate, P95 latency, and usage-known rate.
- [ ] Provide per-domain and per-relation summaries and keep unavailable human full-task quality marked unknown.

### Task 4: Run the four-mode ablation and verify artifacts

**Files:**
- Create: `py/.rag-evaluation/tree-ablation-v1/dev.jsonl`
- Create: `py/.rag-evaluation/tree-ablation-v1/report.json`
- Create: `py/.rag-evaluation/tree-ablation-v1/summary.md`

- [ ] Run A, B1, parent_merge, and typed_edge with zero harness retries and append-only output.
- [ ] Recompute row counts and headline metrics independently from the raw JSONL.
- [ ] State whether the observed differences are retrieval-only or include human-reviewed answer quality; do not make an adoption claim.
- [ ] Run focused tests, `git diff --check`, and redacted artifact scans; do not commit or push until the user reviews the results.

