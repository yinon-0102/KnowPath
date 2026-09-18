# 接口契约完成与验收记录

验收日期：2026-09-19。范围：`docs/API.md` 定义的本地单用户后端；前端不属于本次接口实现范围。

## 功能核对

- 资料：PDF/Markdown/TXT 解析、原文件持久保存、上传与版本幂等、自动/手动 ingest、版本查询、归档、来源读取及评估期间 assisted 标记。
- 图谱：候选准备、七种关系的 Neo4j 原生写入/读回、冲突审查与显式发布、不可变历史查询、层级/深度/来源筛选、确认先修链的范围扩展。
- 学习空间：已发布版本绑定、范围与画像的乐观锁、推断偏好候选与显式确认、学习状态和证据追踪。
- 测验：有来源的题目生成、封存答题版本、确定性评分与不确定结果保护、独立证据去重、提示/查资料证据排除、复核与重置。
- 计划与会话：生成计划、任务更新冲突、会话事件幂等、复测后局部重排；手动完成任务不生成掌握证据。
- 对话：版本限定检索、引用校验、消息/测验持久化任务、同一 Run 重试与重启恢复、取消和资源删除后的迟到写入防护。
- 数据管理：版本采纳及不可变审计时间线、纠错确认、24 小时导出、空间删除、资料级联业务清理和 Neo4j/Qdrant 持久清理任务。
- 通用协议：49 个接口的未知查询字段拒绝、28 个写入接口的未知 JSON/multipart 字段拒绝、游标分页、本地令牌/Origin、统一 request_id 错误响应、SSE 续传、真实依赖健康检查。

## 路由与业务回归映射

路径统一省略 `/api/v1`。实现位于 `py/my_agent_llms/learning/`；测试名称均省略 `py/my_agent_llms/test/test_learning_` 前缀和 `.py` 后缀。

每一项除了表内业务测试，还接受 `http_contract` 的严格字段校验。此表提供可追溯性，不把路由数量当作业务完成率。

| # | 接口 | 主要实现模块 | 业务回归测试 |
| ---: | --- | --- | --- |
| 1 | GET `/health` | `health` | `health`、`http_contract` |
| 2 | POST `/materials` | `materials` | `api`、`raw_materials`、`auto_ingest`、`list_contracts` |
| 3 | GET `/materials` | `materials` | `list_contracts` |
| 4 | POST `/materials/{material_id}/versions` | `materials` | `auto_ingest`、`ingestion_mysql` |
| 5 | GET `/materials/{material_id}/versions` | `materials` | `list_contracts` |
| 6 | GET `/materials/{material_id}` | `materials` | `api`、`material_updates` |
| 7 | GET `/materials/{material_id}/versions/{version_id}/chunks/{chunk_id}` | `source_access` | `source_access` |
| 8 | GET `/runs/{run_id}` | `runs` | `runs`、`runs_mysql` |
| 9 | GET `/runs/{run_id}/events` | `runs` | `runs`、`runs_mysql` |
| 10 | POST `/runs/{run_id}/cancel` | `runs` | `runs`、`model_task_deletion` |
| 11 | GET `/materials/{material_id}/topics` | `graph_queries` | `graph_queries`、`list_contracts` |
| 12 | GET `/topics/{topic_id}/graph` | `graph_queries` | `graph_queries` |
| 13 | POST `/materials/{material_id}/reconcile` | `graph_reconciliation` | `graph_reconciliation`、`graph_worker` |
| 14 | GET `/materials/{material_id}/graph-diff` | `graph_reconciliation` | `graph_reconciliation` |
| 15 | POST `/materials/{material_id}/graph-revisions/{revision_id}/publish` | `graph_reconciliation` | `graph_reconciliation`、`graph_scope_contract` |
| 16 | GET `/learning-spaces` | `spaces` | `list_contracts` |
| 17 | POST `/learning-spaces` | `spaces` | `spaces`、`spaces_mysql`、`space_readiness` |
| 18 | PATCH `/learning-spaces/{space_id}` | `spaces` | `spaces`、`spaces_mysql` |
| 19 | GET `/learning-spaces/{space_id}` | `spaces` | `spaces` |
| 20 | POST `/learning-spaces/{space_id}/scope` | `spaces` | `graph_scope_contract`、`spaces_mysql` |
| 21 | GET `/learning-spaces/{space_id}/profile` | `profile_candidates` | `profile_candidates` |
| 22 | PATCH `/learning-spaces/{space_id}/profile` | `profile_candidates` | `profile_candidates`、`spaces_mysql` |
| 23 | GET `/learning-spaces/{space_id}/state` | `assessments` | `mastery`、`state_persistence` |
| 24 | POST `/learning-spaces/{space_id}/assessments` | `assessments/model_tasks` | `assessment_api`、`question_generation`、`model_tasks_mysql` |
| 25 | GET `/assessments/{assessment_id}` | `assessments` | `assessment_api`、`state_persistence` |
| 26 | POST `/assessments/{assessment_id}/attempts` | `assessments` | `assessment_api`、`state_persistence` |
| 27 | POST `/assessments/{assessment_id}/finalize` | `assessments` | `assessment_api`、`state_persistence` |
| 28 | GET `/assessments/{assessment_id}/result` | `assessments` | `assessment_api`、`grade_reviews` |
| 29 | POST `/learning-spaces/{space_id}/plans` | `planner` | `plans_sessions`、`plans_mysql`、`planner_regressions` |
| 30 | GET `/plans/{plan_id}` | `planner` | `plans_sessions`、`plans_mysql` |
| 31 | PATCH `/plans/{plan_id}/tasks/{task_id}` | `planner` | `plans_sessions`、`plans_mysql` |
| 32 | POST `/plans/{plan_id}/sessions` | `planner` | `plans_sessions`、`plans_mysql` |
| 33 | POST `/sessions/{session_id}/events` | `planner` | `plans_sessions`、`completion_regressions` |
| 34 | POST `/sessions/{session_id}/finish` | `planner` | `plans_sessions`、`plans_mysql` |
| 35 | POST `/learning-spaces/{space_id}/messages` | `messages/model_tasks` | `messages`、`message_retrieval`、`model_tasks_mysql` |
| 36 | GET `/learning-spaces/{space_id}/evidence` | `timeline/assessments` | `list_contracts`、`state_persistence` |
| 37 | POST `/learning-spaces/{space_id}/knowledge-corrections` | `corrections` | `corrections`、`review_audit` |
| 38 | POST `/learning-spaces/{space_id}/knowledge-corrections/{correction_id}/confirm` | `corrections` | `corrections`、`graph_worker` |
| 39 | GET `/learning-spaces/{space_id}/changes` | `timeline` | `timeline`、`completion_regressions` |
| 40 | POST `/materials/{material_id}/ingest` | `material_ingest_worker` | `ingest_commands`、`auto_ingest_mysql` |
| 41 | GET `/learning-spaces/{space_id}/knowledge-updates` | `knowledge_updates` | `knowledge_updates` |
| 42 | POST `/learning-spaces/{space_id}/knowledge-updates/apply` | `knowledge_updates` | `knowledge_updates`、`completion_regressions` |
| 43 | POST `/learning-spaces/{space_id}/state/reset` | `assessments` | `state_persistence`、`review_audit` |
| 44 | POST `/assessments/{assessment_id}/grade-reviews` | `grade_reviews` | `grade_reviews` |
| 45 | POST `/learning-spaces/{space_id}/exports` | `exports` | `exports`、`exports_mysql` |
| 46 | GET `/exports/{export_id}/download` | `exports` | `exports`、`exports_mysql` |
| 47 | PATCH `/materials/{material_id}` | `materials` | `material_updates` |
| 48 | DELETE `/learning-spaces/{space_id}` | `space_deletion` | `space_deletion`、`model_task_deletion` |
| 49 | DELETE `/materials/{material_id}` | `material_deletion` | `material_deletion`、`model_task_deletion` |

## 验证记录

- 最终整套真实依赖回归：**987 passed、38 skipped、0 failed**，耗时 310.53 秒。真实连接 MySQL、Neo4j、Qdrant；38 个 skip 是按后端参数不适用的分支，例如内存/SQLite 参数不能执行 MySQL 行锁和双连接竞争测试。包含这些用例的 MySQL 参数已实际运行。
- 测试仍有一条 Starlette TestClient 对 httpx 的弃用提示，不影响本次通过；没有为消除提示而调整依赖版本。
- 最近针对性 MySQL 回归：16 passed、6 skipped；覆盖发布发生在旧事务快照之后的空间创建、删除竞争、采纳时间线持久化、模型任务并发领取与重启重试。
- 数据库：`uv run alembic current` 为 `0013_material_raw (head)`；`uv run alembic check` 返回 `No new upgrade operations detected`。本轮新增数据使用既有 JSON/outbox 表，无遗漏迁移。
- 共享数据库回归仅领取/清理夹具所属事件、Run 和资源；付费模型凭据在测试进程内置空，并禁用二次 dotenv 加载。
- 提交前检查本轮 68 个变更文件，未发现本地真实密钥或私钥内容；差异空白检查通过。提交按模型适配、图谱/空间、任务/数据生命周期、HTTP/回归、部署文档分组。远程推送结果以最终交付记录为准。

## 运行边界

- 完整持久服务需要同时运行 API、`graph_worker_cli` 和 `model_worker_cli`，配置 MySQL、Neo4j、Qdrant 与模型认证；启动命令见根 README。
- 已验证模型适配协议、错误分支和数据保护；没有调用付费 DashScope 来验证真实题目/回答质量。健康检查的 `configured` 仅表示认证配置存在。
- HTTP 图查询当前从 MySQL 不可变图快照在 Python 中遍历；Neo4j 已保存七类版本化原生关系，参与准备校验与删除。将在线查询迁移为 Neo4j 路径查询属于尚未完成的架构一致性事项，不能声称该架构性能已验收。
- 部分列表先加载匹配记录再分页；尚未进行大规模负载基准。本次验收以首版本地单用户语义与持久正确性为范围。
- 模型任务默认租约 300 秒、每 100 秒续租；每次领取的续租/发布预算 1200 秒（CLI 可设 1–3600），默认最多尝试 3 次。预算耗尽停止续租并拒绝迟到发布，后续领取重试或保留 `MODEL_TASK_TIMEOUT` 失败收尾。该预算不是整个 Run 的总时限，也不能中断阻塞的底层调用；单 worker 长时间阻塞时需要另一个健康 worker 或进程重启。取消由后续心跳观察并停止续租，发布事务仍验证取消/token。
- 独立复审：模型续租、取消、超时和删除生命周期 141 项内存/SQLite 测试通过；没有发现剩余阻断项。
