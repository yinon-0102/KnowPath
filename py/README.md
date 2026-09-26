# KnowPath Python 后端

KnowPath 的 Web 后端，提供 REST API、SSE 事件流和后台任务。前端位置为项目根目录的
`frontend/`，目前预留；本目录包含 Python 服务、完整 Agent 基座、测试和数据库迁移。

## 环境与安装

- Python 3.13 或更高版本，使用本目录的 `.venv`。
- PyCharm 打开整个 `KnowPath/`，解释器选择 `py/.venv/Scripts/python.exe`。
- 包名为 `knowpath_backend`，发行名为 `knowpath-backend`。

在 `KnowPath/py/` 执行：

```powershell
uv sync --locked
```

默认同时安装运行依赖和 pytest；部署环境可使用 `uv sync --locked --no-dev`。
仅在 `.env` 不存在时复制模板，保留已有配置：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

配置 `DASHSCOPE_API_KEY`、MySQL 的 `DATABASE_URL`、Neo4j 连接及密码、Qdrant 地址。
持久运行使用 `LEARNING_PERSISTENCE=sql`、`LEARNING_RETRIEVAL_BACKEND=qdrant`。
默认对话模型为 `qwen-plus`，嵌入模型为 DashScope `text-embedding-v3`、1024 维。
Web 模型选择使用 `LEARNING_CHAT_*`、`LEARNING_EMBEDDING_*`；通用 Agent 基座仍使用
独立的 `LLM_*` 配置，可通过 `core.runtime.load_config()` 读取，默认不会持久化配置。
完整字段见 [.env.example](.env.example)。不要将密钥提交到 Git。

## 数据库

先启动 Docker Desktop，再从本目录启动基础服务并应用迁移：

首次启动需先按 [基础服务说明](../infra/README.md) 配置 `infra/.env` 的 MinIO 凭据，
并将访问凭据同步到本目录 `.env`。

```powershell
docker compose -f ..\infra\docker-compose.yml up -d
uv run alembic upgrade head
uv run alembic current
```

Alembic 读取 `alembic.ini` 同目录的 `.env`，已有进程环境变量优先。现有库升级前应备份；
SQL 模式启动不会自动建表。正常安装和升级统一使用 Alembic。
[init_mysql.sql](init_mysql.sql) 仅是空 MySQL 库的备用初始化快照，不能重复导入现有库；
详情见 [数据库迁移说明](migrations/README.md)。

原有 `knowpath-learning-db` / `learning.db_cli` / `scripts/init_db.py` 和
`learning.vector_indexing` 已保留为维护工具。直接按 ORM 建表不会登记 Alembic 版本，
旧向量脚本固定 `graph_version=1`，因此正常安装升级与资料发布仍使用上面的迁移和后台任务流程。

Neo4j 节点和 Qdrant 向量由图谱后台进程随资料处理写入，无需手动创建业务节点。
数据库名、向量集合前缀和 Docker 卷沿用已有配置，包名调整不迁移数据。

## 启动

### 原始文档与旧文件迁移

使用 `LEARNING_RAW_STORAGE=minio` 时，新上传文档只在 MinIO 保存原始字节；
MySQL 的 `material_raw_files` 保存后端、桶、对象键和 ETag，资料版本保留大小和 SHA-256。
API、两个 worker 和 RAG 维护命令使用相同配置，重启后仍能按引用解析原文件。
对象读取核对长度和 SHA-256；丢失或损坏时报错，不静默回退为另一份内容。

迁移 `0015_material_object_storage` 仅调整表结构，原有 SQL 字节继续可读；
完成数据库备份、MinIO 私有桶初始化和环境配置后，在本目录执行：

```powershell
# 默认仅预览数量，不上传或删除文件。
uv run python -m knowpath_backend.learning.materials.raw_migration
# 上传并回读校验，切换引用；保留 SQL 字节备份。
uv run python -m knowpath_backend.learning.materials.raw_migration --apply
# 验证备份恢复后可选：再次校验对象，清除 SQL 字节，保留全部业务元数据。
uv run python -m knowpath_backend.learning.materials.raw_migration --apply --prune-sql
```

每个版本独立事务，重复执行会跳过已完成项，失败后可重跑。
迁移与删除使用一致的资料锁，删除事务把所有对象引用写入持久化 outbox 后清理 SQL；
graph worker 重试删除对象及其历史版本，只有外部存储全部确认清除才结束删除 Run。
必须持续运行 graph worker；对象存储不可用时删除任务保持待重试。

上传阶段存储失败返回 `503 RAW_STORAGE_UNAVAILABLE`；正常异常回滚会尝试删除新对象。
SQL 与 MinIO 不是同一原子事务：进程被强杀或补偿期间 MinIO 不可用可能留下无引用对象，
日志标识 `RAW_OBJECT_ROLLBACK_CLEANUP_PENDING`，需核对 SQL 引用及待删除 outbox 后运维清理，
不可仅按时间批量删除对象。备份与恢复应同时覆盖 MySQL 和 `infra_keel_minio` 卷。
恢复时暂停 API/worker，恢复相互匹配的数据库、卷和配置，再启动并校验原文件。

已有 MinIO 引用时，不能只改回 `LEARNING_RAW_STORAGE=sql` 或直接降级表结构；
降级迁移会拒绝对象引用及空 SQL 字节。若需整体回退，使用迁移前的完整备份。

真实存储验收使用 `test_learning_minio_live.py`：显式设置
`KNOWPATH_TEST_MYSQL_ADMIN_URL`（具备建库权限的测试实例）和 `KNOWPATH_TEST_MINIO=1`，
以及普通 MinIO 环境变量。测试仅创建并删除随机前缀的专用库和桶。

### 运行进程

在三个终端分别执行，工作目录均为 `KnowPath/py/`：

```powershell
uv run python -m uvicorn knowpath_backend.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m knowpath_backend.learning.graph_worker_cli
uv run python -m knowpath_backend.learning.model_worker_cli
```

| 进程 | 职责 |
| --- | --- |
| API 服务 | HTTP 请求、鉴权、业务状态、运行记录（Run）查询和 SSE 事件流 |
| 图谱后台进程 | 资料解析、Neo4j/Qdrant 快照准备、纠正与删除清理 |
| 模型后台进程 | 持久化的出题与对话任务、重试和取消检查 |

两个 `*_cli.py` 是后台进程的启动入口，需要保留。已删除交互式终端界面和 `knowpath` 聊天命令，
Agent 基座完整保留；CLI 中可复用的装配和会话授权逻辑已移到 `core.runtime` 与 `core.permissions`。
空闲后台进程没有业务输出属于正常情况；持续出现 `STORAGE_UNAVAILABLE` 表示存储连接或
数据库结构仍有问题，应检查配置、Docker 状态和迁移版本。

PyCharm 可直接选择项目根目录 `.run/` 中的 **KnowPath Backend** 组合配置。
该配置同时启动三个后端进程；Docker 和迁移需提前准备。默认保持单个 API 进程。

三项服务均加载 `.env`。浏览器或 API 客户端使用 `X-Local-Token`：可显式设置
`LEARNING_LOCAL_TOKEN`，否则 API 生成并复用 `.learning-token.local`。
浏览器来源需包含于 `LEARNING_ALLOWED_ORIGINS`。健康检查：
`http://127.0.0.1:8000/api/v1/health`；未认证时只显示基本状态，携带令牌后可查看依赖情况。

资料上传后的正常流程为：解析和图谱准备、审核候选、发布快照、创建学习空间、诊断和学习。
仅上传成功不代表资料已经可以绑定到新空间。具体请求见 [接口文档](../docs/API.md)。

## 回归测试

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
uv run python -m pytest .\knowpath_backend\test -q
Remove-Item Env:PYTHON_DOTENV_DISABLED
```

未设置 `LEARNING_TEST_*` 时，普通测试使用内存或临时 SQLite，外部存储集成测试跳过。
这些变量只能指向独立、可清理的测试库，不能指向日常资料库。迁移测试覆盖空库升级、
增量升级、回退和记录保留。模型契约测试使用测试适配器，不验证真实付费模型的效果或可用性。

Windows 路径、SQLite 句柄和测试解释器问题已修复；没有符号链接创建权限时仅跳过对应测试。
目录整理后的隔离全量回归为 1367 项通过、217 项跳过；完整 OpenAPI 与数据库结构
快照保持一致。真实模型与临时数据库验收也已通过，具体结果见
[目录调整记录](../docs/implementation/2026-09-19-backend-layout.md)。

## 代码边界与来源

学习后端按职责组织，HTTP 请求入口位于 `learning/api/routers/`：

```text
knowpath_backend/
  learning/
    main.py                  # ASGI 启动入口
    config.py                # 环境配置与本地令牌
    state.py                 # 服务装配与现有业务门面
    api/
      application.py         # create_app、生命周期、注册路由
      dependencies.py        # 读取每个应用实例的服务
      middleware.py          # 来源、令牌、幂等键与请求编号
      errors.py              # HTTP 错误响应映射
      routers/               # 按业务拆分的请求处理函数
    materials/               # 上传、解析、来源访问、资料删除
    spaces/                  # 学习空间、档案、时间线、导出
    assessments/             # 诊断、出题、评分、复核、掌握度
    plans/                   # 计划、任务与学习会话
    conversations/           # 对话、答案生成与上下文记忆
    knowledge/               # 图谱准备、发布、查询、纠正与更新
    rag/                     # 检索和向量索引
    providers/               # 模型协议、调用与能力适配
    persistence/             # 唯一 ORM Base、仓储、共享工作单元
    workers/                 # 后台任务执行、租约与进程实现
  agents/ core/ context/ memory/ planning/ tools/ verify/ tdd/ workspace/
  bench/
  test/
```

查找一个接口时，从 `api/routers/` 的处理函数进入业务模块，再查看
`persistence/` 中的存储实现。`state.py` 仍作为已有业务门面，跨领域协作没有
在本次目录调整中重新设计。请求结构的 `schemas.py` 与所属业务放在一起。

启动命令和 PyCharm 配置保持兼容：`learning.graph_worker_cli`、
`learning.model_worker_cli`、`learning.vector_indexing` 是薄启动入口，
具体实现分别在 `workers/` 与 `rag/`。`learning.db_cli` 仍是维护入口。
`learning/indexes.py` 保留原有通用 Neo4j/Qdrant 适配器，与当前业务索引实现职责不同。
历史实现记录中的旧文件路径反映当时目录；当前定位以本节为准。

- `knowpath_backend/learning/`：学习业务、存储适配和三个服务入口。
- `core/`、`agents/`：通用模型客户端、Agent 执行循环、回调及无界面装配。
- `context/`、`memory/`：上下文预算、去重、分层记忆、摘要、检索、冲突处理和持久化。
- `tools/`、`planning/`、`verify/`、`tdd/`、`workspace/`：工具注册、计划、验证和工作区基座。
- `bench/`、`exp_hybrid_recall.py`：原有评测与检索实验。
- `knowpath_backend/test/`：后端回归测试及其辅助代码。
- `migrations/`、`alembic.ini`：版本化数据库结构。
- `init_mysql.sql`：空库初始化快照。

完整 Agent 基座和非界面测试已从以下本地分支恢复。仅终端 UI、`chat.py` 和界面专用测试
退出当前代码；原始版本仍保存在分支
`codex/archive-agent-before-web-cleanup-20260919`，提交
`0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`。
基座中的 Shell/文件工具仍然存在，但当前 Web API 不注册或开放这些工具。
当前 Web 对话已接入基座的上下文预算、摘要与检索组件，持久化以学习业务 SQL 消息为准；
通用 Agent 的自动核心记忆晋升、Shell/文件工具不会通过 Web 对话开放。
项目源于 Keel，保留其 [MIT 许可证和版权声明](LICENSE)。

## Web 对话记忆

已完成的消息为各学习空间提供持久记忆，记忆范围限定于当前学习范围和资料绑定。
最近几轮对话、长度受限的抽取式摘要，以及相关的跨会话片段会进入现有上下文引擎。
服务重启后从 SQL 数据库重建记忆召回；失败或尚未完成的消息不会进入记忆。
历史片段始终作为不可信上下文处理，不会直接修改学习者的成绩或档案。

`LEARNING_CONTEXT_BUDGET_TOKENS` 默认为 16000，允许范围为 1024–65536。
它限制整个提示输入的估算词元数量，包括资料来源、历史消息和系统文本。
如果必须保留的用户问题本身超过预算，请求将以 `CONTEXT_BUDGET_EXCEEDED` 失败。
这里使用的是估算值，并非模型服务商分词器的精确计数。目前每次请求都会重建
用于记忆召回的 TF-IDF 索引；对话记录规模很大时，需要改用带持久索引的存储。

## 隔离环境中的真实验收

启动 Docker 并配置好 DashScope 密钥后，在 `py/` 目录执行：

```powershell
uv run python scripts/accept_learning_backend.py --live
```

此命令会进行有限次数的真实模型调用，并创建临时 MySQL、Neo4j 和 Qdrant 容器，
通过本机回环地址上的随机端口访问。它只从 `.env` 读取模型相关配置，不使用日常数据库。
脚本会在全新数据库上应用迁移，用测试资料验证完整学习流程，最后仅删除自身创建并标记的容器。
脱敏后的 JSON 报告默认写入 `%TEMP%/knowpath-live-acceptance.json`。
可通过 `--report PATH` 指定其他报告位置。HTTP 路由通过 FastAPI 的 `TestClient`
进行验证，不涵盖反向代理或浏览器界面。
