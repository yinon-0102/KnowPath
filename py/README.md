# KnowPath Python Backend

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
默认对话模型为 `qwen-plus`，Embedding 为 DashScope `text-embedding-v3`、1024 维。
Web 模型选择使用 `LEARNING_CHAT_*`、`LEARNING_EMBEDDING_*`；通用 Agent 基座仍使用
独立的 `LLM_*` 配置，可通过 `core.runtime.load_config()` 读取，默认不会持久化配置。
完整字段见 [.env.example](.env.example)。不要将密钥提交到 Git。

## 数据库

先启动 Docker Desktop，再从本目录启动基础服务并应用迁移：

```powershell
docker compose -f ..\infra\docker-compose.yml up -d
uv run alembic upgrade head
uv run alembic current
```

Alembic 读取 `alembic.ini` 同目录的 `.env`，已有进程环境变量优先。现有库升级前应备份；
SQL 模式启动不会自动建表。正常安装和升级统一使用 Alembic。
[init_mysql.sql](init_mysql.sql) 仅是空 MySQL 库的备用初始化快照，不能重复导入现有库；
详情见 [migrations/README.md](migrations/README.md)。

原有 `knowpath-learning-db` / `learning.db_cli` / `scripts/init_db.py` 和
`learning.vector_indexing` 已保留为维护工具。直接按 ORM 建表不会登记 Alembic 版本，
旧向量脚本固定 `graph_version=1`，因此正常安装升级与资料发布仍使用上面的迁移和 worker 流程。

Neo4j 节点和 Qdrant 向量由 graph worker 随资料处理写入，无需手动创建业务节点。
数据库名、collection 前缀和 Docker 卷沿用已有配置，包名调整不迁移数据。

## 启动

在三个终端分别执行，工作目录均为 `KnowPath/py/`：

```powershell
uv run python -m uvicorn knowpath_backend.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m knowpath_backend.learning.graph_worker_cli
uv run python -m knowpath_backend.learning.model_worker_cli
```

| 进程 | 职责 |
| --- | --- |
| API | HTTP 请求、鉴权、业务状态、Run 查询和 SSE |
| graph worker | 资料解析、Neo4j/Qdrant 快照准备、纠正与删除清理 |
| model worker | 持久化的出题与对话任务、重试和取消检查 |

两个 `*_cli.py` 是后台进程的启动入口，需要保留。已删除交互式终端界面和 `knowpath` 聊天命令，
Agent 基座完整保留；CLI 中可复用的装配和会话授权逻辑已移到 `core.runtime` 与 `core.permissions`。
空闲 worker 没有业务输出属于正常情况；持续出现 `STORAGE_UNAVAILABLE` 表示存储连接或
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

Windows 上完整基座测试仍有归档原代码中即可复现的路径、SQLite 句柄、`python3` 命令和
符号链接权限问题；本次恢复未隐藏这些失败。具体结果见
[恢复记录](../docs/implementation/2026-09-19-agent-foundation-restoration.md)。

## 代码边界与来源

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
当前 Web 对话使用 `learning/` 的资料检索和历史快照，尚未接入基座的完整 Memory/Context 流程。
项目源于 Keel，保留其 [MIT LICENSE 和版权声明](LICENSE)。
