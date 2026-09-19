# Web 后端与旧 Agent 的代码边界

> 本文保留裁剪前的审计结论。最终范围已修正为完整保留 Agent 基座及维护工具，仅删除终端界面；
> 当前边界以 [后端 README](../../py/README.md) 和
> [恢复计划](2026-09-19-agent-foundation-plan.md) 为准。

本轮仅盘点职责、源码依赖和清理范围，不删除、移动代码，不修改依赖或启动行为。

## 结论与核验范围

当前运行的是独立的学习 Web 后端，旧终端 Agent 与它同包分发，但不在 Web 的生产调用链上。
这不是“旧 CLI 套了一层 HTTP”，也不能把旧 `tools/` 下的全部功能视为 Web 必需工具。

以以下三个生产入口进行 Python AST 导入分析，包含函数内导入、相对导入和包初始化：

- `knowpath_backend.learning.main`
- `knowpath_backend.learning.graph_worker_cli`
- `knowpath_backend.learning.model_worker_cli`

本地可达模块共 59 个，包含顶层空包初始化；其余均位于 `learning/`。
`learning/` 共 60 个 Python 模块，其中 `db_cli`、`vector_indexing` 是另外调用的维护入口。
没有发现 `learning/` 对旧 Agent 目录的导入，也没有发现其中使用 `import_module` 或 `__import__` 动态加载旧模块。
这是对当前源码的静态核验，不保证第三方插件或将来新增代码的依赖，也不等于每个被导入的类都实际参与业务执行。

## Web 产品需要保留的能力

以下路径均相对于 `py/knowpath_backend/learning/`。
它们是后端业务服务或适配器，并非浏览器直接加载的 Python 工具。

| 产品能力 | 当前实现 | 处理边界 |
| --- | --- | --- |
| HTTP API、校验、本地认证、健康检查 | `main.py`、`api.py`、`http_contract.py`、`health.py`、各类 `*_schemas.py` | 保留 |
| 上传、原文件存储、解析和版本管理 | `materials.py`、`ingestion.py`、`material_ingest_worker.py`、`repositories.py` | 保留，不替换成旧 ReadFile/WriteFile |
| 学习空间、范围和画像 | `spaces.py`、`space_repository.py`、`profile_candidates.py` | 保留 |
| 图谱准备、审核发布、纠正、版本查询 | `graph_reconciliation.py`、`graph_preparation.py`、`graph_worker.py`、`graph_queries.py`、`corrections.py` | 保留 |
| 资料检索、Embedding、Qdrant | `vector_retrieval.py`、`model_adapters.py` | 保留，限定资料版本和学习范围 |
| 学习问答、提示、上下文快照 | `messages.py`、`message_generation.py`、`source_access.py` | 保留，不依赖旧终端对话历史 |
| 出题、答题、评分、证据和掌握度 | `question_generation.py`、`assessments.py`、`assessment_grading.py`、`mastery.py`、`grade_reviews.py` | 保留；当前开放题保守标记未验证，不把旧 verify 当成语义评分器 |
| 学习计划和学习会话 | `planner.py`、`planner_policy.py` | 保留，不使用旧 `planning/todo.py` 的任务清单 |
| 后台执行、重试、取消、SSE、事务 | `model_tasks.py`、`runs.py`、`run_repository.py`、`unit_of_work.py`、各 repository | 保留 |
| 删除、导出、时间线、知识更新 | `material_deletion.py`、`space_deletion.py`、`exports.py`、`timeline.py`、`knowledge_updates.py` | 保留 |

架构文档中的 `import_material`、`search_material`、`get_topic_graph`、`generate_assessment`、
`grade_attempt`、`record_learning_evidence`、`build_or_update_plan`、`get_progress` 是业务能力边界。
当前代码通过服务方法实现这些职责，未将它们注册进旧 `ToolRegistry`。
写入评分证据、更新学习状态等仍应由服务事务控制，不能直接交给模型自由调用。

## 有 CLI 名称但不是旧终端界面的入口

| 路径 | 用途 | 处理边界 |
| --- | --- | --- |
| `learning/graph_worker_cli.py` | 启动解析、图谱准备与外部清理进程 | 生产必需；可另行调整命名，但不能删除其职责 |
| `learning/model_worker_cli.py` | 启动测验、消息生成进程 | 生产必需 |
| `learning/db_cli.py`、`py/scripts/init_db.py` | 直接按 ORM 建表的维护入口 | 非常驻服务；不要与 Alembic 升级等同，后续可统一初始化路径 |
| `learning/vector_indexing.py` | 手动构建资料向量索引 | 非正常启动必需；固定 `graph_version=1` 的旧逻辑需复核，不能当成任意已发布版本的通用重建工具 |
| `py/migrations/`、`py/alembic.ini` | 数据库版本升级 | 必须保留 |
| `py/init_mysql.sql` | 全新 MySQL 空库初始化快照 | 保留，不在已有数据库重复执行 |
| 根目录 `.run/`、`infra/` | IDE 运行配置、数据库容器 | 保留 |

`learning/indexes.py` 中的 `Neo4jGraphStore`、`QdrantVectorStore` 当前仅由包初始化导出，未发现生产调用方。
实际图谱与向量流程使用 `graph_preparation.py`、`vector_retrieval.py`。
这属于学习模块内部的早期适配代码，另列清理候选；`storage.py` 中的 `GraphSettings` 仍被健康检查使用，不能一起删除。

## 明确的旧终端产品代码

`py/knowpath_backend/cli/` 的 17 个 Python 文件属于旧终端交互：

- 入口与配置：`app.py`、`__init__.py`。
- 终端显示：`banner.py`、`console.py`、`chat_view.py`、`help_view.py`、`markdown_render.py`、`scrollback_renderer.py`、`status_bar.py`、`theme.py`、`thinking.py`。
- 输入与交互：`prompt.py`、`completer.py`、`key_listener.py`、`live_session.py`。
- 终端审批：`permission.py`、`permission_grants.py`。

配套的 `py/chat.py` 和 `pyproject.toml` 中的 `knowpath = "knowpath_backend.cli.app:main"` 也是旧聊天入口。
对仅交付 Web 学习产品的目标，这些是明确的迁出或移除候选；当前还未执行移除。

不能只删除 `cli/`：`bench/default_factory.py` 调用了其 `load_config` 和 `build_agent`，
`test_package_installation.py` 当前也明确验证旧 CLI 入口。移除时必须同步处理这些引用、命令入口和文档。

## 旧 Agent 的其他代码，不等同于终端 UI

以下目录目前均不被 Web 生产链引用；其中一些可复用，但“可复用”不等于“已接入”。

| 目录或文件 | 当前职责 | 建议边界 |
| --- | --- | --- |
| `core/`、`agents/` | 通用 Agent 生命周期、多厂商客户端、推理与工具循环 | 单独保留为旧运行时或迁出；不称为当前 Web 核心 |
| `tools/base.py`、`registry.py`、`chain.py` | 工具协议、注册、编排 | 不属于纯终端 UI，但当前 Web 不依赖；`chain.py` 本身还含终端打印 |
| `tools/builtin/bash.py`、`read_file.py`、`write_file.py`、`edit_file.py`、`list_dir.py`、`grep.py`、`glob.py` | 通用 Shell 与本地代码工作区操作 | 不纳入学习 Web 工具白名单 |
| `tools/builtin/search.py` | 外部网页搜索 | 不等于范围内资料检索，当前学习流程不需要 |
| `tools/builtin/calculator.py`、`recall.py`、`remember.py` | 计算与旧 Agent 记忆工具 | 当前 Web 未接入，不因名称通用就视为必需 |
| `memory/`、`context/` | 旧 Agent 分层记忆、事实图谱、上下文预算 | 当前 Web 使用自己的存储、检索和快照；作为原计划复用项另行决策 |
| `planning/` | Agent 编码任务 Todo | 不是学习计划模块，可随旧运行时迁出 |
| `verify/`、`tdd/`、`workspace/` | 编码结果核验、测试驱动编程、本地文件工作区 | 不属于当前学习产品主流程 |
| `bench/`、`py/exp_hybrid_recall.py` | 旧 Agent 评测、检索实验 | 非生产必需；作为研究与评测资产单列 |

旧 `cli/app.py` 的 `build_agent` 会注册计算器、Read/Edit/Write、LS、Grep、Glob、Bash 和 Todo。
这套注册逻辑没有被 Web 调用，也不应直接迁入 Web。

## 测试、依赖和配置的边界

- 保留 `test_learning_*.py`、资料上传等测试辅助模块、安装与迁移测试中的 Web 部分。
- 旧 CLI/Agent 测试随旧代码迁出或删除；不能将整个 `test/` 当作垃圾，也不能仅凭测试文件名机械切分。
- Web 直接使用 `fastapi`、`httpx`、`pydantic`、`python-dotenv`、`pypdf`、`sqlalchemy`、`neo4j`、`qdrant-client`；还有 `uvicorn` 启动、`python-multipart` 上传、`pymysql` 数据库驱动和 Alembic 迁移的运行依赖。
- `prompt-toolkit`、`rich` 属于旧终端依赖候选；`anthropic`、`google-genai`、`openai` 等在当前代码中供旧 `core/llm.py` 使用。Web 默认模型适配器使用 `httpx`。
- 删除依赖必须结合运行测试、打包入口及传递依赖重新锁定，不能仅凭“没有直接 import”就卸载。
- 保留 `LEARNING_*`、`DASHSCOPE_API_KEY`、数据库配置。旧 `LLM_*`、`SERPAPI_API_KEY`、`MY_CHAT_STORAGE` 与 `~/.my_companion` 是旧路径相关配置；迁出旧运行时后再清理说明，不能删除用户本地数据或密钥文件。
- 保留原始 LICENSE 和版权归属，不随命名或产品裁剪删除。

## 与原架构的差异及后续任务范围

`docs/ARCHITECTURE.md` 第 4.1 节提出复用 Agent、Memory、Context 和 ToolRegistry，
第 4.2 节则明确排除旧 CLI、Shell 和文件编辑能力。
当前实现满足“Web 不调用旧 CLI/代码工作区”的边界，但没有把第 4.1 节的复用方案接入生产。
因此不能宣称旧运行时已经完成领域适配，也不能以清理目录为由悄悄放弃该设计目标。

建议下一步首先只处理明确的旧终端界面及其直接入口、专用测试、文档和依赖。
通用 Agent 基座单列为“迁出归档”或“按明确需求领域化”的架构决策；没有实际业务需求前不重新接入。
学习模块内部的早期索引适配器、手动索引命令和重复初始化入口另列维护任务。
现有数据库、Docker 数据卷、业务 API 契约和用户配置不属于本轮裁剪范围。

完成未来裁剪的验收应包括：API 和两个 worker 的导入及启动、空库迁移、安装包加载、
上传到发布及学习流程回归，并证明生产流程不再导入旧终端代码。
