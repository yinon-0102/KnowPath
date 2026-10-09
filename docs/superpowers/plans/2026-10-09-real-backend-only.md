# 真实后端单一模式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 删除 KnowPath 当前前端中的示例/演示数据与手动鉴权流程，让页面启动后自动连接本地后端并只展示真实功能。

**Architecture:** Node 前端静态服务在回环地址提供一次性读取本地启动配置的接口，前端将令牌放入当前标签页 `sessionStorage` 后直接调用既有 API。应用状态只保留 live 数据，示例数据生成器、示例测评和示例分支全部移除；根目录旧首页与旧 Python 静态服务器删除，PowerShell 启动器统一启动 Node 前端。

**Tech Stack:** Vanilla ES modules, Node `http` server, Node test runner, PowerShell launcher.

**执行状态（2026-10-09）：** 已实施并验证，首页视觉重构暂缓。前端只加载真实后端数据；删除运行时示例空间、资料、模拟图谱/计划/测评/评分/问答和手动令牌弹窗。根目录旧首页及 Python 静态服务器已删除，启动器统一使用 Node 前端。设计范围同步记录于 `../specs/2026-10-09-auto-local-auth-design.md`。

**补充决策：** 保留原有 PDF 缓存中的 `live` 键前缀及已提交后台任务的会话日志键，避免已有真实资料副本及待处理任务入口失联；应用不再有数据模式切换。自动连接请求合并并设置 8 秒超时，服务状态页提供重新自动连接。dotenv 变量引用和引号转义与后端一致；自定义相对令牌路径以 `py` 为基准，非标准 API 启动目录应配置绝对路径。

**验证记录：** `npm.cmd --prefix frontend run check`、全部 182 项前端测试、PowerShell 解析与隔离启动函数检查、`git diff --check` 均通过。用当前本机配置在临时回环端口验证鉴权和 13 个公开资源；静态资源不包含令牌，私有路径返回 404。完整应用启动测试使用隔离后端响应，验证先鉴权、真实资料渲染及删除入口；未在真实后端执行删除或调用模型。临时服务已关闭，没有替用户启动持久服务。

---

### Task 1: Lock automatic authentication and demo removal with tests

**Files:**
- Modify: `frontend/tests/server.test.mjs`
- Modify: `frontend/tests/source-flow.test.mjs`
- Modify: `frontend/tests/store.test.mjs`
- Modify: `frontend/tests/workbench.test.mjs`
- Modify: `frontend/tests/learning.test.mjs`
- Create: `frontend/tests/real-mode.test.mjs`

- [x] **Step 1: Write the failing server test**

  Add a `createFrontendServer({ projectRoot })` fixture using a temporary `py/.env` and `.learning-token.local`, then assert `GET /__knowpath/local-auth` returns the API base and token with `Cache-Control: no-store`; assert a foreign `Host` receives `403` and the token file path remains `404`.

- [x] **Step 2: Run the focused server test and verify it fails**

  Run `node --test --test-name-pattern="local auth" frontend/tests/server.test.mjs`.
  Expected: FAIL because the endpoint and configurable project root do not exist.

- [x] **Step 3: Write the failing frontend contract tests**

  Assert `app.js` has no `makeDemo`, `loadDemo`, `demo-mode`, `reset-demo`, `connection-form`, or `local_token` strings; assert it initializes in live mode and calls the automatic auth endpoint. Assert `learning.js` has no `demoQuestion` or `demoQuestions`; assert `workbench.js` has no `demoCurrentPlan`, `demoProgress`, or `demoLearningRecord`; assert store exports no demo factory.

- [x] **Step 4: Run the focused contract tests and verify they fail for the expected old-demo strings**

  Run `node --test frontend/tests/real-mode.test.mjs`.
  Expected: FAIL with matches found in the current source, proving the tests detect the existing behavior.

---

### Task 2: Add the loopback automatic-auth endpoint

**Files:**
- Modify: `frontend/server.mjs`
- Modify: `frontend/tests/server.test.mjs`

- [x] **Step 1: Implement testable local configuration loading**

  Export `createFrontendServer({ projectRoot } = {})`; add an internal dotenv parser that reads `py/.env`, honors `LEARNING_LOCAL_TOKEN`, otherwise reads `LEARNING_LOCAL_TOKEN_FILE` or `py/.learning-token.local`; reject empty or whitespace-containing tokens. Keep the API base fixed at `http://127.0.0.1:8000/api/v1`.

- [x] **Step 2: Implement `GET /__knowpath/local-auth`**

  Serve `{ token, api_base }` only for `GET`/`HEAD` on the same loopback Host. Apply existing security headers plus `Cache-Control: no-store`; return a generic `503` JSON error when configuration is unavailable and never include the token or file path in logs or errors. Keep the existing public file allowlist unchanged.

- [x] **Step 3: Run server tests**

  Run `node --test frontend/tests/server.test.mjs`.
  Expected: PASS.

---

### Task 3: Make the frontend live-only and remove all demo behavior

**Files:**
- Modify: `frontend/src/app.js`
- Modify: `frontend/src/store.js`
- Modify: `frontend/src/workbench.js`
- Modify: `frontend/src/learning.js`
- Modify: `frontend/src/progress.js`
- Modify: `frontend/src/workspace-view.js`
- Modify: `frontend/src/api.js`
- Modify: `frontend/tests/real-mode.test.mjs`
- Modify: existing frontend tests that assert demo behavior

- [x] **Step 1: Add automatic auth bootstrap before app data loading**

  Replace the demo default with a single live state. Read the existing session token if present; otherwise fetch `/__knowpath/local-auth`, validate the response shape, store the token in `sessionStorage`, and then call `refreshData()`. Keep token values out of rendered HTML and error messages. A failed bootstrap renders a retryable real-service error.

- [x] **Step 2: Remove connection UI and mode controls**

  Delete the connection modal, token input form, `settings` connection action, demo switch, reset-demo action, mode pill copy, demo privacy/help sections, and any “connect local service” buttons. Replace stale connection actions with the existing service-health route or a retry action.

- [x] **Step 3: Simplify data and mutation paths to backend-only**

  Make `data()` return `state.live`, remove `DEMO_KEY`, `MODE_KEY`, demo storage, demo upload/PDF branches, demo space creation, demo plan generation, demo task completion, and demo scope updates. Keep local `sessionStorage` only for auth and transient UI state.

- [x] **Step 4: Delete local learning simulation**

  Remove local question generation, local answer grading, demo assessment state, and demo-only renderer branches from `learning.js`. Remove demo plan/progress/record constructors from `workbench.js`. Make progress records always render the real session, assessment, and source actions.

- [x] **Step 5: Run focused frontend tests**

  Run `npm.cmd --prefix frontend test -- --test-name-pattern="real backend|live|learning|workbench|progress|workspace"`.
  Expected: PASS after updating tests to use backend fixtures only.

---

### Task 4: Remove the legacy homepage and unify startup

**Files:**
- Delete: `index.html`
- Delete: `scripts/serve_frontend.py`
- Delete: `scripts/test_serve_frontend.py`
- Modify: `start-local.ps1`
- Modify: `README.md`
- Modify: `frontend/README.md`

- [x] **Step 1: Update launcher expectations**

  Change `start-local.ps1` to validate `frontend/server.mjs`, launch Node with `npm.cmd`/`node`, verify the `X-KnowPath-Frontend` header and `/__knowpath/local-auth` readiness, and remove `-OpenBrowser` fragment-token behavior. The launcher must continue starting only API and frontend processes and must not print token values.

- [x] **Step 2: Delete legacy files**

  Remove the root legacy page and Python static server/tests after launcher references are gone. Keep the Node frontend public allowlist as the only browser entrypoint.

- [x] **Step 3: Update documentation**

  Describe only real backend startup, automatic local auth, material upload/parse, study spaces, plans, sessions, assessment, RAG, and deletion. Remove all demo-mode instructions, sample-data claims, and manual-token steps.

- [x] **Step 4: Run launcher/static checks**

  Run `npm.cmd --prefix frontend run check` and `npm.cmd --prefix frontend test`.
  Expected: exit code 0 with no demo contract failures.

---

### Task 5: Full verification and cleanup

**Files:**
- Modify: only files required by failing verification or documentation consistency.

- [x] **Step 1: Search for forbidden demo and manual-auth remnants**

  Run `rg -n "demo|示例|演示|connection-form|local-token|local_token|DEMO_KEY|MODE_KEY|makeDemo|loadDemo|demoQuestion|demoCurrentPlan|demoProgress|demoLearningRecord" frontend/src frontend/index.html frontend/README.md start-local.ps1 README.md`.
  Expected: no user-facing demo or manual-token code; only explicitly documented migration history may remain.

- [x] **Step 2: Run all frontend tests**

  Run `npm.cmd --prefix frontend test`.
  Expected: all tests pass.

- [x] **Step 3: Verify served assets and auth endpoint**

  Start the Node frontend on an unused local port, request `/`, every public module, and `/__knowpath/local-auth`; verify no token appears in static HTML/JS and the endpoint returns a token only through the loopback request.

- [x] **Step 4: Run `git diff --check` and inspect the final diff**

  Expected: no whitespace errors, no accidental token content, and no unrelated changes.
