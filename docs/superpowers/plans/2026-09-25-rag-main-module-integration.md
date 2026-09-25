# RAG Modules to Main Integration Plan

> **For agentic workers:** Execute this plan task-by-task with verification after each module.

**Goal:** Integrate the current RAG implementation into `main` as separate, reviewable commits using English prefixes with Chinese explanations.

**Architecture:** Preserve the existing commit history for already committed RAG foundations and add the remaining working-tree changes in module commits. Keep ordinary retrieval, tree navigation, evaluation/reporting, and documentation separable; exclude secrets, logs, temporary scripts, and local evaluation artifacts.

**Tech Stack:** Python, pytest, Git worktrees, Qdrant integration code, Markdown documentation.

---

### Task 1: Commit integration plan

- [ ] Add this plan under `docs/superpowers/plans/`.
- [ ] Verify the plan itself contains no credentials or local paths that must remain private.
- [ ] Commit with `docs:补充RAG主分支分模块整合计划`.

### Task 2: Commit ordinary RAG working-tree changes

**Files:** modified ordinary RAG runtime/registry/vector/CLI and corresponding tests.

- [ ] Stage only ordinary RAG runtime and test files.
- [ ] Run the focused ordinary RAG test suite.
- [ ] Commit with `fix:完善普通RAG运行链路`.

### Task 3: Commit tree navigation implementation

**Files:** tree navigation, section/unit indexing, evidence packets, and their tests.

- [ ] Stage only tree navigation implementation and tests.
- [ ] Run tree navigation tests.
- [ ] Commit with `feat:完善树形RAG导航链路`.

### Task 4: Commit evaluation and reporting tools

**Files:** evaluation analysis/reporting modules, launchers, and evaluation tests.

- [ ] Stage only evaluation tooling and tests.
- [ ] Run evaluation unit tests that do not call external services.
- [ ] Commit with `feat:补齐RAG评测分析工具`.

### Task 5: Commit research and plan documentation

**Files:** current RAG research, plans, and design documents.

- [ ] Stage Markdown documentation only.
- [ ] Commit with `docs:整理RAG研究与评测记录`.

### Task 6: Validate and push main

- [ ] Run the complete local RAG-focused test set available in the integrated worktree.
- [ ] Verify no secret, `.env`, log, cache, or temporary files are staged.
- [ ] Fast-forward or merge the verified integration branch into `main`.
- [ ] Push `main` and verify `origin/main` points to the resulting commit.
