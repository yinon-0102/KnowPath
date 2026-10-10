# 首页与学习顺序第三版实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将首页改为“当前学习桌面”，删除重复的学习空间目录，并把学习顺序弹窗改为宽松的两栏路线编辑器。

**Architecture:** 复用现有 `workbench`、空间、资料和笔记索引数据，首页只在当前选定空间上下文中渲染路线、资料和笔记。排序弹窗保留现有计划排序控制器与 API，只调整渲染结构和 CSS，不改变排序校验、幂等、前置约束或服务启动方式。

**Tech Stack:** 原生 JavaScript ES modules、HTML 模板字符串、CSS、Node test runner、Vite-free 本地静态服务器。

---

### Task 1: 首页当前空间桌面

**Files:**
- Modify: `frontend/src/app.js` (`overview`, `spaceCard` 相关首页渲染)
- Modify: `frontend/src/styles.css`（首页路线、资料和笔记布局）
- Test: `frontend/tests/home-overview.test.mjs`

- [x] **Step 1: 写失败测试**：断言首页模板包含当前空间选择器、当前空间资料与最近笔记标题，不包含首页空间目录标题；断言 `needs_replan` 状态主按钮显示查看路线而非开始学习。
- [x] **Step 2: 运行测试确认失败**：`npm.cmd test -- frontend/tests/home-overview.test.mjs`，应因新测试文件或断言缺失失败。
- [x] **Step 3: 实现首页**：复用已有 `currentSpace()`、`workbench.snapshot()`、`nextLearningStep()` 和 `state.live.materials`；空间选择器改变时更新 `state.selected`、清空旧路线/笔记缓存并触发当前路由重绘。资料只显示当前空间 bindings 中的材料，按 `created_at` 降序；笔记仅显示 notebook 索引中的章节状态与更新时间，空状态写明“尚未生成学习笔记”。删除首页空间列表和重复空间入口。
- [x] **Step 4: 添加 CSS**：路线卡分左右两栏，空间选择器与计划状态同一上下文；资料与笔记为下方两栏，窄屏堆叠；保留现有蓝灰色 token、焦点样式和 reduced-motion 规则。
- [x] **Step 5: 运行测试确认通过**：`npm.cmd test -- frontend/tests/home-overview.test.mjs`。
- [x] **Step 6: 仅提交首页模块**：已与排序编辑器相关前端改动合并为一个可运行的 UI 提交 `78dda79`，未提交后端文件。

### Task 2: 学习顺序两栏弹窗

**Files:**
- Modify: `frontend/src/plan-order.js`
- Modify: `frontend/src/styles.css`
- Test: `frontend/tests/plan-order.test.mjs`

- [x] **Step 1: 写失败测试**：断言渲染结果包含安排方式说明、完整节点槽位、固定节点原因、原文章节和可访问的上下移按钮；断言按钮和节点不被旧的紧凑单列结构覆盖。
- [x] **Step 2: 运行测试确认失败**：`npm.cmd test -- frontend/tests/plan-order.test.mjs`。
- [x] **Step 3: 修改渲染结构**：保留现有状态字段与事件属性；左侧渲染三种安排方式和资料优先级，右侧渲染按预览/当前位置排序的完整节点列表，固定节点留在原槽位并显示原因；预览结果就地显示变化与冲突，不重复创建独立长列表。
- [x] **Step 4: 修改 CSS**：弹窗最大宽度约 1000px；正文两栏、内部滚动、固定头尾；节点行最小触控尺寸 44px，长标题可换行；窄屏上下排列，底部按钮可换行。
- [x] **Step 5: 运行测试确认通过**：`npm.cmd test -- frontend/tests/plan-order.test.mjs`。
- [x] **Step 6: 仅提交排序模块**：已与首页相关前端改动合并为一个可运行的 UI 提交 `78dda79`，未提交后端文件。

### Task 3: 集成验证与远程推送

**Files:**
- Verify: `frontend/src/app.js`, `frontend/src/plan-order.js`, `frontend/src/styles.css`, related tests
- Update: `docs/superpowers/specs/2026-10-10-home-and-order-dialog-design.md`

- [x] **Step 1: 运行前端静态检查**：`npm.cmd run check` 通过。
- [x] **Step 2: 运行完整前端测试**：`npm.cmd test`，304 项通过。
- [x] **Step 3: 检查工作区**：`git diff --check` 通过；相关 UI 文件已包含在 `78dda79`。
- [x] **Step 4: 浏览器回归**：临时前端服务器已启动并关闭，1440px 页面无横向溢出；真实 API 当前不可连接，因此未声称真实数据页面通过。静态布局与前端测试已验证。
- [x] **Step 5: 提交设计文档更新**：已在 `59f4f61` 提交设计、计划与视觉验证产物。
- [x] **Step 6: 推送当前分支**：已推送 `codex/backend-integrity-repairs` 到 `origin`。
