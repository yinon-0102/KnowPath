# 学习路线、Markdown 阅读与流式输出验收

实施范围对应 `docs/superpowers/plans/2026-10-09-study-reading-streaming-polish.md`，保留现有蓝灰配色、真实数据及双模式学习流程。

## 页面与行为

- 空间顶部仅保留一条当前路线，展示真实完成比例；历史轮次折叠且不再重复当前路线。计划任务列表保留执行和记录入口。节点显示去除重复的原文章节编号，后端编号不变。
- Markdown 原始资料根据 MIME 或文件名识别，默认排版阅读，提供源码切换。有序列表、引用、表格、分隔线及围栏代码均适配；原始 HTML 转义，链接仅允许 HTTP(S) 和页内引用。PDF 仍显示原文件，TXT 保持纯文本。
- 节点追问和空间问答消费 SSE `message.delta`，候选正文标注依据待核对，最终回答替换草稿，避免重复拼接。断线续接同一 Run，重试 reset、失败及取消清空草稿。
- 讲义使用模型 provider 的流式 JSON 输出，每个完整小节通过结构和引用 ID 校验后发布累计快照。客户端显示逐步到达的小节，最终通过讲义读取接口再次审计；生成未结束时不启用讲义追问。
- 来源、当前任务、Run 所属及 worker lease 均校验；失效、停止、重试和资料删除撤回暂存正文。跨空间同源测评也记录辅助交付。逐字符问答输出合并为首片段立即、后续最多约 80ms/128字符一批及尾部 flush，避免每个字符写一条数据库事件。
- 来源跳转在异步步骤后核对原节点快照及路由，迟到引用不能切换新节点的阅读模式。引用直接读取目标来源，避免重复先读其它来源。

## 验证证据

- `npm.cmd --prefix frontend run check`：通过。
- `npm.cmd --prefix frontend test`：245 项通过。
- 后端九组相关 pytest：205 通过，19 跳过，1 条既有 Starlette 弃用提示。覆盖 `test_study_lesson_streaming`、`test_study_lessons`、`test_learning_messages`、`test_learning_message_provider`、`test_learning_model_task_leases`、`test_learning_model_task_deletion`、`test_document_scope_deletion`、`test_message_document_scope`、`test_learning_review_audit`。
- 流式专项覆盖完成前 section 交付、HTTP SSE、失效 lease、同 Run 自动重试 reset、来源删除及旧 Run 清理、取消后的晚回调，以及共享来源辅助审计。SQL/内存删除均有覆盖；跳过项目不能视为已验证。
- 前后端只读复审通过，引用跳转竞态已修正并补回归。
- 浏览器在现有 5173 服务检查真实 Python 空间与 Markdown 资料：1440px 与 390px 均无整页横向溢出；计划页当前路线由重复展示降为一条；标题、代码及阅读/源码切换可用，无 JS 异常。
- 浏览器用已保存真实讲义的两个小节作为受控增量回调验收流式展示：小节能逐步呈现，生成期追问禁用。此项未调用新的真实模型生成，也未写入业务示例或任务记录。真实 provider 的调用路径、SSE 契约和结束前交付通过后端隔离测试验证。
- 页面截图及一次性 CDP 验收脚本存于 `.runtime.local/study-polish-*`，不作为业务内容发布。

## 手动运行要求

本次没有启动、停止或重启项目服务，没有新增迁移或常驻服务。请手动重启 API 和 model worker 使后端代码生效，再刷新前端。旧讲义仍可阅读；使用“重新生成”观察新增流式小节。正在运行的旧后端进程不能作为最新流式代码已加载的证明。
