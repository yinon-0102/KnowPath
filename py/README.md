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

## 后端日志与问题定位

API、模型 worker、图谱 worker 使用统一的标准库日志。正常启动后，控制台的
stderr 默认显示中文单行日志，先说明正在做什么、处理结果和必要的耗时/数量，
再附请求或任务编号；不再把线程、调用链等全部参数堆到正文里。
文件始终使用 UTF-8 JSONL，每行一条 JSON，保留原有字段并新增中文 `message`，便于阅读和按字段筛选。
默认文件位于本项目的 py/logs，文件名分别为 api-<PID>.jsonl、
model-worker-<PID>.jsonl、graph-worker-<PID>.jsonl。默认路径与启动工作目录无关。
两个 worker 的 --once 仍只向 stdout 输出原有 job_claimed JSON，诊断日志不会混入。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| KNOWPATH_LOG_LEVEL | INFO | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| KNOWPATH_LOG_FORMAT | text | 控制台 text 或 json；文件格式固定为 JSONL |
| KNOWPATH_LOG_DIR | 本项目 py/logs | 自定义目录；显式空值关闭文件日志，相对路径基于进程工作目录 |
| KNOWPATH_LOG_MAX_BYTES | 10485760 | 单文件轮转阈值，默认 10 MiB，允许 256 字节至 1 GiB |
| KNOWPATH_LOG_BACKUPS | 5 | 每进程文件的备份数，允许 1–100 |

在 py/.env 中调整这些配置后重启 API 和对应 worker。临时排查可设置
KNOWPATH_LOG_LEVEL=DEBUG；希望控制台也输出 JSON 时设置 KNOWPATH_LOG_FORMAT=json。
正常健康检查、Run 查询和空闲轮询只写 DEBUG，失败仍然可见。worker 连续轮询失败时，
记录首次及每第 15 次失败；恢复时记录累计次数，避免存储不可用时反复刷屏。
文件创建失败会写 logging.file.unavailable 并继续使用控制台。

### 不熟悉系统时怎么看

每行依次为「UTC 时间、级别、服务、中文说明、追踪编号」。`注意` 表示原来的 WARNING，
`错误` 表示 ERROR；模型任务服务负责模型相关后台任务，知识图谱服务负责整理知识关系。
先看中文说明即可了解工作进展；需要排查时，再用行尾的完整编号和 `事件` 搜索 JSONL。

以下为示意，省略时间及编号：

~~~text
信息 [接口服务] 查找候选资料；找到候选资料：12
信息 [接口服务] 模型已返回响应（尚未校验答案）；模型：qwen-plus；耗时：1.25 秒；用量：未提供（不代表零消耗）
注意 [模型任务服务] 本次未完成，等待重试；任务内容：生成问答回复；第 2 次尝试
注意 [接口服务] 等待工具结果超时；后台执行可能仍在继续，请先核对结果，避免重复操作
~~~

- 「已返回响应」只表示收到了模型响应，不保证答案正确；后续校验和问答结果会单独说明。
- 「问答流程已结束」不等于回答完整；资料依据不足、只能回答部分问题、需要澄清都会分别解释。
- 「已申请重试」表示发出了状态变更请求，最终是否生效仍以任务记录为准；不会把重试写成已完成。
- 模型用量仅展示实际提供的计数。部分计数缺失时显示「总量未知」，不会估算费用或把未知补成零。
- 文本只展示常用指标与关联编号；原有 `event`、`span_id`、`parent_span_id`、异常位置等仍保留在 JSONL。
  `message` 是由已脱敏的元数据生成的阅读辅助，不是新的状态判定依据，也不应拿来统计调用次数。
- 需要在控制台查看完整字段时仍使用 `KNOWPATH_LOG_FORMAT=json`；无需新增配置。
  未识别的新事件显示中性的「系统事件」和原事件名，不会仅凭名称猜测业务已成功。

常见错误会解释含义，并在已知情况下提示检查方向；不会调用模型解释日志，也不会读取或输出问题、
答案或文档正文。既有普通日志保留原文脱敏行为；第三方组件的原始消息仍按原策略省略。
变更仅影响重启后的新日志，已有日志文件不会被改写。

### 从请求追踪到后台任务

1. 从响应头 X-Request-ID 或错误响应的 error.request_id 获取请求编号。
2. 查找同一 request_id 的 http.request.* 与 command.* 事件，定位操作、HTTP 状态、
   安全错误码、耗时，以及命令返回的 run_id。
3. 按 run_id / job_id 检索 model-worker 或 graph-worker 日志，检查 attempt、
   observed_status、error_code、backoff_seconds 和 duration_ms。
   新任务会把原始 request_id 持久化到 outbox，重启及解析→图谱交接后仍可关联；
   已存在的旧任务可能没有 request_id，此时使用 run_id / job_id。

在项目根目录使用 PowerShell 搜索（替换示例编号）：

~~~powershell
Get-ChildItem .\py\logs\*.jsonl* | Select-String -SimpleMatch '你的 request_id 或 run_id'
Get-ChildItem .\py\logs\*.jsonl* | Get-Content -Encoding utf8 |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.level -in @('ERROR', 'WARNING') } |
  Select-Object timestamp, service, message, event, request_id, run_id, job_id, error_code
~~~

主要事件：

| 事件 | 说明 |
| --- | --- |
| api.build.* / api.started / api.shutdown.* / api.stopped | API 装配与生命周期 |
| http.request.completed / failed / disconnected | 请求整体生命周期，含 SSE 最后一个响应块、断连和后台处理 |
| command.completed / failed / replayed | 命令执行、错误和幂等重放；不记录幂等键或输入 |
| model.http.* / model.generate.* / model.generate_json.* / model.stream.* | HTTP 尝试次数、状态、模型名称、耗时以及生成校验结果 |
| worker.attempt.started / finished / failed | 任务编号、资源编号、尝试次数、执行后观察到的 outbox 状态 |
| worker.retry.* / worker.failure.requested / worker.lease.renewal_failed | 重试、退避、终止失败与续租异常 |
| material.parse.* / graph.prepare.* / material.cleanup.failed | 资料解析、图谱准备、外部清理 |
| rag.retrieve.* / rag.rerank.* / rag.verify.* / rag.answer.* | 检索、重排、生成及验证、整体 RAG 的耗时和失败位置 |
| rag.stage / rag.result | 候选数、重排数、上下文数、引用数和回答状态 |

时间戳使用 UTC ISO 8601，耗时单位为毫秒。所有事件均带 service、pid、logger、level。
request_id、run_id、job_id 仅在对应上下文存在时提供。路由记录模板，不记录查询参数、
原始 URL、客户端地址或文件名；在路由匹配前被认证等中间件拒绝时显示 <unmatched>。
HTTP status_code 在响应头成功发送前为空；response_bytes 统计已交给 ASGI send 的字节，
response_complete 表示最后一个响应块已发送，不能替代业务成功状态。流式响应即使已有
200 响应头，后续异常仍记录 http.request.failed。

worker.attempt.finished 只表示本次执行已返回，成功与否应看 observed_status。
pending 表示等待重试，processing 表示仍受租约控制，superseded 表示已被新尝试接管。
无法读取状态时记 unavailable，不据此重试业务或覆盖结果。command.completed 出现在事务
正常退出之后；任务内部 *.requested 事件可能先于事务提交，日志不作为事务审计凭据。

### 模型与工具动作追踪

模型动作统一使用 `model.call.*`，工具动作统一使用 `tool.call.*`。这些事件默认 INFO
可见；失败为 ERROR，超时、拒绝及结果未知为 WARNING。原有 `model.http.*`、
`model.generate.*` 和 RAG 阶段事件继续保留，用于观察整体业务操作；统计模型调用次数时
只按 `model.call.started` 的 `model_call_id` 去重，不把阶段事件重复计数。

| 字段 / 事件 | 含义 |
| --- | --- |
| span_id / parent_span_id | 当前操作及父操作编号；阶段、模型、工具可串成调用链 |
| action_id / model_call_id / tool_call_id | 系统生成的动作编号；同一次动作的开始和结束共用编号，不使用供应商返回的工具 ID |
| observation_scope=http | 直接 HTTP 适配器或 RAG journal 观察到的一次请求尝试；应用重试产生新编号 |
| observation_scope=sdk | 通用 MyLLM 或函数调用 Agent 的一次 SDK 调用；SDK 内部自动重试不展开，不能据此推算真实 HTTP 次数 |
| attempt / retry_reason | 普通模型重试次数；Agent 因空响应且长度受限而追加调用时，记录新编号及 empty_length_limit |
| revision_index / stage_call_index | RAG 初稿为第 0 轮、修订为第 1 轮；生成与验证分别从 1 计数，传递到 HTTP 事件并保留在 journal |
| usage / usage_status | 仅保留供应商实际返回的非负整数 token 计数；observed 表示至少一个计数可用，缺少的项仍未知 |
| finish_reason | 供应商返回且通过白名单的停止原因；缺失时不猜测 |
| model.call.completed / failed / cancelled / unknown | 调用边界的完成、异常、取消或结果未知；completed 不代表答案正确或已发布 |
| model.call.validation | RAG 对同一模型调用的后续校验；content_passed 只表示内容 JSON 可用，passed/failed 表示后续契约检查结果 |
| model.operation.* / model.embedding.* / model.reranking.* | 本地输入检查、解析和返回结果校验的操作结果；可在没有实际模型请求时失败 |
| tool.call.approval | 是否需要审批及 approved / denied / not_required / callback_unavailable；描述现有审批流程，不改变审批策略 |
| tool.call.executing | 实际开始执行；可与执行前被拒绝的动作区分 |
| tool.call.completed | 工具函数返回；result_status=returned 不保证业务成功，既有 ❌ 返回约定记为 error_reported |
| tool.call.rejected / failed / timed_out / cancelled / unknown | 拒绝、抛出异常、超时、批次中止且未提交执行，或批次中止后结果未知 |
| origin_model_call_id | 产生这次工具调度的模型调用编号；无法关联时为空，绝不编造 |

未提供 usage 时记录 `usage_status=unknown` 和空对象，不能把未知当成零消耗；估算输入量、
预算预留也不能当成供应商账单。SDK 内部重试、未完成流和超时请求的最终计费仍需供应商侧核对。
汇总用量时也应按 model_call_id 去重，取该调用最新可用的计数；validation 事件再次携带的
usage 属于同一次调用，不可与完成事件重复累加。
工具超时记录 `execution_may_continue=true`，表示后台执行可能继续；日志不会声称已强制终止。
同一动作最多一个终态，超时或 journal 封口后的迟到结果不会覆盖终态或补写为成功。
批次中止时，已提交线程池的排队动作也可能继续执行，统一记 unknown；不会把“尚未开始”
误记为“已取消”。通用工具直接调用仍记录实际抛出的异常。

RAG 的请求线程、复用 HTTP 线程以及并行工具线程只传递日志元数据；不会复制数据库事务、
SQL 会话或其他 ContextVar。复用线程逐次绑定与恢复上下文，避免不同请求的编号串线。
独立调用模型、工具时若没有 HTTP 请求上下文，request_id / run_id 可以不存在。

例如，在项目根目录检索某次模型调用及其关联工具（替换动作编号）：

~~~powershell
$callId = '日志中的 model_call_id'
Get-ChildItem .\py\logs\*.jsonl* | Get-Content -Encoding utf8 |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.model_call_id -eq $callId -or $_.origin_model_call_id -eq $callId } |
  Select-Object timestamp, event, model_call_id, tool_call_id, origin_model_call_id,
                stage, revision_index, duration_ms, usage_status, result_status
~~~

此处追踪的是动作、耗时、状态和安全计数，不保存模型思维链、提示词、回答、工具参数或工具
结果全文。原有 CLI 实时文本输出仍保持既有行为；动作事件不把这些文本复制进日志。

### 隐私、轮转与扩展

新增业务事件只记录明确挑选的 ID、枚举、计数和耗时，不记录用户问题、答案、资料正文、
提示词、请求体、响应体、HTTP 头、密钥或租约令牌。异常保留类型和调用位置（文件、行号、
函数），不记录异常消息、源码行或局部变量。已知依赖的自由文本消息会被省略，避免 SQL、
HTTP、PDF 解析或 Uvicorn 预格式化回溯泄露内容；原始日志级别与 logger 仍保留。
旧式普通日志会做凭据脱敏，但新增代码仍应使用 observability.log_event / span 并明确
选择安全字段，不能依赖正则识别任意正文。DEBUG 也不会开启请求或模型内容输出。

每个进程单独轮转，避免多进程同时重命名同一文件。轮转限制仅作用于同一个 PID 的文件；
重启遗留的旧 PID 文件不会自动删除，长期运行需由运维按保留期归档或清理。
日志目录应限制访问权限，日志采集器可直接读取 JSONL；本次未引入日志查询 UI 或远端平台。
配置只由三个生产入口执行；直接调用 create_app 的测试/嵌入场景由宿主配置 handler。
已有宿主 handler 会保留，它们的输出格式和脱敏策略仍由宿主负责。
程序内调用 Alembic 时沿用宿主已有的根日志 handler；独立迁移命令也不会禁用已经加载的
应用 logger，避免执行迁移后模型及业务日志静默消失。
