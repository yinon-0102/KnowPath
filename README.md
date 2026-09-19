# KnowPath

KnowPath 是一个学习应用，项目根目录统一管理 Python 后端、前端工作区、
项目文档和本地基础服务。

## 项目目录

```text
KnowPath/
├── frontend/       # 前端应用（预留）
├── py/             # Python 后端及其 uv 虚拟环境
├── docs/           # 架构与接口文档
└── infra/          # Docker Compose 基础服务
```

## Python 后端

后端项目及其虚拟环境均位于 `py/`。Python 包名为 `knowpath_backend`，
发行名为 `knowpath-backend`。后端提供 Web API 和两个后台任务进程，并保留
可复用的 Agent 基座，包括上下文、分层记忆、工具、规划和验证能力。
已移除的仅是交互式终端界面及其 `knowpath` 聊天命令。

HTTP 请求处理函数位于 `py/knowpath_backend/learning/api/routers/`。
学习业务按领域分组：检索位于 `rag/`，模型适配位于 `providers/`，
仓储位于 `persistence/`，后台执行位于 `workers/`。下方原有启动命令仍然有效。
完整目录说明见 [后端目录指南](py/README.md#代码边界与来源)。

```powershell
cd C:\Users\27202\Desktop\KnowPath\py
uv sync --locked
```

要运行完整的持久化学习后端，请仅在 `py/.env` 不存在时将 `py/.env.example`
复制为该文件，设置 `LEARNING_PERSISTENCE=sql`，并配置数据库连接和 DashScope 密钥。
模板默认使用 `LEARNING_PERSISTENCE=sql` 和 `LEARNING_RETRIEVAL_BACKEND=qdrant`；
也可选择 `keyword` 作为备用检索方式。请保留已有本地配置。
启动 Docker 后，在 `py/` 目录执行数据库迁移。Alembic 读取与 `alembic.ini`
同目录的 `.env`，进程环境变量优先：

```powershell
uv run alembic upgrade head
```

在三个终端中分别启动以下进程，工作目录均为 `py/`：

```powershell
uv run python -m uvicorn knowpath_backend.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m knowpath_backend.learning.graph_worker_cli
uv run python -m knowpath_backend.learning.model_worker_cli
```

图谱后台进程负责资料解析、Neo4j/Qdrant 数据准备、纠正以及外部存储中的资料清理。
模型后台进程负责持久化的测评出题、对话生成任务及有限次数的重试。
上传的知识必须经过审核和发布，才能绑定到新的学习空间。

API 会加载 `py/.env`。可设置 `LEARNING_LOCAL_TOKEN`，或由 API 自动创建
`py/.learning-token.local`；客户端通过 `X-Local-Token` 请求头发送令牌。
令牌文件已被 Git 忽略。浏览器请求来源必须匹配 `LEARNING_ALLOWED_ORIGINS`。
未认证的 `/api/v1/health` 请求仅返回基本状态；认证后会报告依赖服务的可用性，
但不会返回连接字符串或密钥。默认嵌入模型为 DashScope `text-embedding-v3`，维度为 1024。

在 PowerShell 中执行以下回归测试命令，无需使用通配符展开脚本路径：

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
uv run python -m pytest .\knowpath_backend\test -q
Remove-Item Env:PYTHON_DOTENV_DISABLED
```

测试目录覆盖 Web 服务及保留的 Agent 基座。需要显式配置外部数据库的测试，
会在缺少相应 `LEARNING_TEST_*` 变量时跳过；这些变量只能指向可清理的临时测试存储。
契约测试使用结果确定的模型适配器，不代表真实付费模型服务的可用性或回答质量已经通过验证。

## 在 PyCharm 中启动（Windows）

在 PyCharm 中打开整个 `KnowPath/` 目录。共享运行配置位于 `.run/`，
选择 **KnowPath Backend** 可同时启动 API、图谱后台进程和模型后台进程，
也可以分别启动或调试各个进程。配置使用 `py/.venv/Scripts/python.exe` 作为解释器，
以 `py/` 为工作目录，并启用 SQL 持久化。首次配置新检出的项目时，
请先在 `py/` 执行 `uv sync --locked`。使用其他操作系统时，
需要将各运行配置中的解释器路径改为对应系统的虚拟环境可执行文件。

运行后端前，请先启动下方的 Docker 服务，并执行 `uv run alembic upgrade head`。
数据库连接和模型凭证配置在 `py/.env` 中，三个进程都会自动加载。
不要将凭证写入共享运行配置。组合配置仅并行启动三个后端进程，
不会启动 Docker、执行数据库迁移或启动前端。`frontend/` 目前为预留目录。
停止后端时，请在 PyCharm 的运行或服务窗口中停止全部三个进程。

API 监听地址为 `http://127.0.0.1:8000`，基本健康检查地址为
`http://127.0.0.1:8000/api/v1/health`。

## 本地基础服务

在项目根目录执行：

```powershell
docker compose -f .\infra\docker-compose.yml up -d
```

Compose 配置提供 MySQL、Neo4j 和 Qdrant 服务。如果宿主机端口已被占用，
只需修改端口映射中的宿主机端口，并同步更新后端连接配置。

## 项目文档

- [架构文档](docs/ARCHITECTURE.md)
- [接口文档](docs/API.md)
- [后端说明](py/README.md)
- [基础服务说明](infra/README.md)

清理前的完整源码（包括已移除的终端界面）保存在本地分支
`codex/archive-agent-before-web-cleanup-20260919`，对应提交为
`0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`。Agent 基座已恢复到工作区，
当前 Web 请求仍通过学习业务流程处理。原项目署名和许可保留在 [许可证文件](py/LICENSE) 中。
