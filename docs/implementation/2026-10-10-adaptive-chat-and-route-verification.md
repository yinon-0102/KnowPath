# 学习路线、阅读侧栏与自适应对话验收

## 实施内容

- 完整学习路线保持原位，由 IntersectionObserver 控制独立浮栏；过渡只改变透明度和位移，展开使用对话框，关闭后恢复焦点与滚动位置。
- 连接和提交前读取运行中 API 的 OpenAPI 契约，缺少 `reading_context` 或 `answer_mode` 时提示手动重启 API 与模型 worker。422 显示字段、校验原因和类型；后端日志不记录用户问题或凭据。
- 新消息默认 `answer_mode=auto`。配置的模型执行语义分类，资料问题使用当前有效范围及真实引用，普通交流使用模型知识；资料不足时允许明确标注的模型补充。检索故障保留失败状态，不假装资料缺失。
- 原文和讲义问答保持资料版本与会话隔离。资料及补充正文由应用分区，避免模型格式变化造成无归因的流式文本。
- 相关测评帮助允许正常回答，但记录为辅助；明确无关交流不因旧资料历史污染测评。保留隐藏题目、资料删除、租约、取消及晚到响应保护。
- 首个流式片段的辅助审计与交付原子提交；若片段已交付，最终完成失败也不能撤销用户已收到帮助的事实。未交付片段的事务失败则回滚审计。
- 提交失败保留输入，POST 确认后才增加已发送消息。重新连接刷新权威数据，同时保留有效阅读 DOM、PDF 浏览上下文、阅读位置和草稿；范围或绑定版本变化时重新加载依据。

## 浏览器与前端验证

`npm.cmd --prefix frontend run check` 通过；完整前端测试 **273 passed，0 failed，0 skipped**。

使用专用 Edge headless 浏览器，在现有 5173 页面验证 1440×1000 与 390×1000 视口：

- 路线边界连续往返四次，路线高度分别稳定为 270.96875 / 256.5625 px，页面高度稳定，阅读 DOM 未替换。
- 展开和关闭路线后恢复原滚动位置；无水平溢出、运行时异常或观察到的 50 ms 以上长任务。
- 重新连接后 reader、PDF iframe 与 contentWindow 身份保持一致，iframe 重载 0 次，页面滚动 550→550，输入草稿保留。
- 重新连接确实刷新 spaces、materials、workbench、progress、state 和 knowledge-updates；浏览器受控响应仅用于页面验收，未提交真实学习或模型任务。
- 侧栏 `general` / `mixed` 两类回答均在首个增量显示归因和说明，第二段正文增量追加，最终消息保留标签；PDF 重载 0 次，新输入草稿跨增量和完成事件保留。

验收脚本与日志位于 `.runtime.local/adaptive-route-browser.mjs`、`.runtime.local/reconnect-browser.mjs`、`.runtime.local/adaptive-sse-browser.mjs`、`.runtime.local/adaptive-frontend-tests.log`。

## 后端验证

项目 `.venv` 执行 23 个相关 pytest 模块，最终结果 **451 passed、69 skipped、0 failed**，92.93 秒。覆盖自适应/provider、消息、原文与讲义、文档范围与持久化、RAG、Run、来源访问、辅助审计、删除和 API 工作流。条件跳过项未计为通过；配置模型的路由与分区流式端口使用受控 provider 验证。

新增回归覆盖普通聊天不检索、同上下文连续追问、伪造引用拒绝、模型补充分区、JSON 字段正反顺序、资料历史后无关交流不误标、未展示题目不进入分类、取消/删除/重试及首片段交付事务。首片段失败回滚与最终完成失败保留已交付辅助记录分别在内存和 SQLite 仓储验证。

测试日志：`.runtime.local/adaptive-backend-tests.log`。唯一警告为现有 Starlette TestClient 的 httpx 弃用提示。相关修改的 `git diff --check` 通过。

## 运行边界与手动生效步骤

未自动启动或重启任何项目服务，未创建数据库表或迁移。验证时 API 进程仍是 2026-10-09 22:28 启动的旧进程，其契约缺少侧栏所需字段，因此页面会明确提示版本不兼容。

请手动重启后端 API 和模型 worker，再刷新前端并重新连接。无需迁移数据库。启动入口仍为 `knowpath_backend.learning.main:app` 与 `knowpath_backend.learning.model_worker_cli`，配置沿用 `py/.env`。

纯资料 RAG 仍使用既有同步核验链：完成生成、证据核验和必要修订后，再通过 SSE 分块发布已核验文本。通用回答、原文/讲义回答和模型补充使用增量输出。本次没有真实付费模型质量或外部 MySQL 测试库的全量验收，浏览器局部受控响应不作为这些能力的验证。
