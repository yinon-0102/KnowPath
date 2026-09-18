# 显式资料入库与持久化恢复

## 范围与实现

按接口文档继续补齐 POST /materials/{material_id}/ingest。原实现返回 queued，却创建已成功的空 Run；现校验 version_id 后在同一事务保存 material_ingest Run、不可变图谱候选、graph.prepare outbox 和幂等响应。复用现有独立 graph worker 的租约、重试、取消、Neo4j/Qdrant 准备及读回验证。

请求使用严格模型，仅接受非空字符串 version_id，必须提供 Idempotency-Key。版本不存在或不属于资料返回 404，未解析完成或没有来源片段返回 409 MATERIAL_NOT_READY。同键不同请求返回 409 IDEMPOTENCY_CONFLICT；同键重放原接收响应及 Run，即使任务后来已失败或取消。失败/取消后用新键可重新启动；同版本、同发布基线已有 queued/running 普通候选时合并为该任务。

在资料锁下读取当前发布基线，始终使用请求指定的资料版本。候选准备成功只进入 pending_review，保留旧发布快照和旧学习空间绑定，需要显式发布。跨版本变更仍需要审核冲突。同版本重建沿用已审核快照，不撤销知识纠错。

API 的 SQL 启动恢复保留 material_ingest，由独立 worker 领取。API 不自动运行外部索引任务，需与 worker 配置同一个 SQL 数据库。

## 事务和测试

新增 memory、SQLite、MySQL 回归：严格 HTTP 输入、幂等重放、进程重建、并发合并、任务失败和取消后的新键重试、事务回滚、指定版本和审核隔离。

独立审查发现嵌套 _execute 会在失败的事务内重试。故障注入回归复现了任务存在而 outbox 缺失的问题，随后提取不含重试的 _stage，由最外层统一回滚和重试，新增断言验证废弃 Run 不会遗留。

启动恢复测试先强制 memory 导入入口，并把 SQL active_ids 限定为该测试创建的两个 Run，不处理开发库其他任务。相关模块回归结果：88 passed、5 skipped、1 warning。

## 未完成边界

上传仍同步解析；此批只补齐已持久化来源的显式图谱/索引入库。原始文件持久化、auto_ingest=false 的真正延迟解析、解析失败后的原文件重试，以及上传后自动串联任务仍待后续实现，不计作已完成。结构化图谱仍由标题与片段生成，未增加语义关系推断。本次没有新增数据库列或迁移。

## 最终验证

完整 learning 回归：650 passed、32 skipped、1 warning（180.98 秒），启用真实 MySQL、Neo4j、Qdrant 测试连接；向量测试使用确定性模型，未调用付费 DashScope。随后调整 HTTP 测试的 Run 跟踪清理，模块复查 28 passed、2 skipped、1 warning。警告为已有 Starlette/httpx 弃用提示。独立复核在事务修复后无新增可操作问题。
