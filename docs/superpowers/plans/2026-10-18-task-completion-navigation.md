# Task Completion Navigation Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans. User has approved this design and its execution.

**Goal:** 完成保存后可靠进入下一步；响应丢失时准确核实原请求，持久保留恢复入口。

**Architecture:** 后端返回原请求完成回执和轻量下一步；只读查询严格核对幂等指纹及当前完成事件。前端保存、结果核实、导航分开，导航不等待工作台、累计进度、知识状态或更新请求。撤销、版本冲突、离开页面与迟到响应均需保持原有边界。

**Tech Stack:** Python/FastAPI/SQLAlchemy；原生 JavaScript；pytest/node:test/Playwright。

## Scope and invariants

- 只修复问题三。保留问题一长事务及问题二学习保存状态，不重置数据、不改数据库参数。
- 保留原 PDF、49 个任务、25 轮测评、125 份答案、全部原会话和保护文件。
- 未知结果不换幂等键；不能只凭 task.completed 判定本请求成功；409 需用户重新确认。
- 工作树隔离开发；主目录同步前比较基线哈希；不提交、不推送。

## Task 1: Backend receipts and read-only recovery

Files: plans/service.py, plans/workbench.py as needed, state.py, api/routers/plans_sessions.py; corresponding backend tests.

- [x] Write failing tests for exact-key recovery, unknown, undo/new event, current continuation and no side effects.
- [x] Run tests and confirm expected failures.
- [x] Add completion_receipt and reliable next_action; add POST completion-status using original body/key, without writing or scanning heavy progress projections.
- [x] Verify memory/SQLite, API contract and relevant lifecycle tests.

## Task 2: Frontend completion state

Files: frontend/src/workbench.js, api.js, tests/workbench.test.mjs.

- [x] Write and run failing tests for lost response, bounded read-only recovery, reload/deduplication, superseded receipt and same-key retry.
- [x] Implement persistent saving/querying/unknown/saved/navigation pending state and recovery API.
- [x] Preserve 409 explicit reconfirmation, immutable request identity and undo ordering.
- [x] Run existing controller tests.

## Task 3: App navigation and persistent recovery UI

Files: frontend/src/app.js, learning.js, tests/route-integration.test.mjs, learning.test.mjs.

- [x] Reproduce successful PATCH followed by failed or hanging aggregate reads in app orchestration tests.
- [x] Capture route before write; navigate from receipt without awaiting aggregate reads; late responses never steal changed routes.
- [x] Persist recovery actions for assessment and manual completion; show saving/result unknown/navigation pending separately.
- [x] Verify task/session/assessment/replan/final summary destinations and explicit recovery after reload.

## Task 4: Integration verification

- [x] Review spec compliance then implementation quality; fix material findings.
- [x] Run relevant backend/frontend suites and source checks; synchronize only verified files after baseline comparison.
- [x] Restart only required service after checking live process command lines.
- [ ] Real UI fault injection: failed/hanging refresh, lost completion response, delayed response/route change, double click, reload/recovery, reversible undo and final summary.
- [x] Read back original task/assessment/answer records and protected files; write final evidence report with practical limits.

## Final verification note

实现与相关测试已完成。真实数据库只读恢复与数据完整性验证通过；其余浏览器故障矩阵在隔离内存环境通过。真实数据库全故障矩阵受原有长事务影响未全部完成，故上项保留未勾选。详见同目录 2026-10-18-task-completion-navigation-report.md。
