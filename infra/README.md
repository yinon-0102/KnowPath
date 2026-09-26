# Local Persistence Services

从 `KnowPath/` 根目录执行：

```powershell
if (-not (Test-Path .\infra\.env)) { Copy-Item .\infra\.env.example .\infra\.env }
# 按下文填写所有存储凭据、同步后端配置，再启动。已有数据卷先阅读迁移步骤。
docker compose -f .\infra\docker-compose.yml config --quiet
docker compose -f .\infra\docker-compose.yml up -d
docker compose -f .\infra\docker-compose.yml ps
```

若当前在 `KnowPath/py/`，文件路径改为 `..\infra\docker-compose.yml`。
容器名中的 `-1` 是 Compose 实例编号；应确认状态为 running/Up，而非 created。

| 服务 | 默认宿主机端口 | 用途 |
| --- | --- | --- |
| MySQL 8.4 | 3306（仅本机） | 业务数据、来源和图谱快照、Run/事件、outbox 任务 |
| Neo4j 5.26 | 7474、7687（仅本机） | 按版本准备的知识图谱 |
| Qdrant | 6333、6334（仅本机） | 按模型和资料版本隔离的向量索引 |
| MinIO | 9000、9001（仅本机） | 原始文档 S3 API、管理控制台 |

端口已被占用时，可修改 Compose 映射左侧的宿主机端口，并同步 `py/.env` 的连接配置。
不要通过删除 Docker 卷解决端口冲突；卷中保存已有数据。
全部存储端口显式绑定 `127.0.0.1`；远程访问应通过受控隧道或经认证的 TLS 代理。
本机 HTTP 连接不提供 TLS 加密；API key 不能替代远程部署中的 TLS。

## 凭据配置与已有数据卷迁移

新安装必须在 `infra/.env` 填写 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、
`NEO4J_PASSWORD`、`QDRANT_API_KEY` 和 `MINIO_ROOT_PASSWORD`；MinIO 用户名也不能为空。
使用密码管理器为各项生成独立的高熵随机值（建议至少 32 个随机字节、URL-safe 字符），
Neo4j 密码至少满足镜像的默认长度要求。示例文件故意留空，Compose 会拒绝缺失或空凭据，
不再内置弱密码。包含 `$`、`#`、空格等字符的 dotenv 值应使用单引号，
避免 Compose 插值；不要把实际凭据提交到 Git、命令行参数或日志中。

**已有卷不要直接更换 `.env` 中的 MySQL/Neo4j 密码并认为已轮换。**
MySQL 初始化环境变量和 Neo4j 的初始认证配置不会覆盖已持久化的用户密码。
先备份并确认恢复方法，将当前仍然有效的密码放入本地 `infra/.env`，使应用恢复一致配置；
这些值只作为迁移过渡，已知弱密码应在维护窗口中通过各数据库的认证管理功能轮换。
MySQL 的应用用户与 root 账户需分别更新，Neo4j 需更新已有 `neo4j` 用户；
用交互式管理工具完成变更，避免密码进入 shell 历史，然后同步两个 `.env` 并重启客户端。
不要删除卷、重新初始化数据库或使用 `down -v` 来轮换密码。

Qdrant 的 API key 是运行时配置：设置新随机 key 并同步所有 API/worker 客户端，
在维护窗口通过 Compose 重建该服务后才启用认证。数据卷保持不变，未携带 key 的请求
应被拒绝。回环端口限制同样需要重建对应存储容器才生效，代码更新不会修改正在运行的容器。
所有步骤就绪后再执行开头的 `up -d`；它会重建配置变化的服务。
即使只启动 MinIO，也需为 Compose 文件中的必填变量提供有效配置。

### 同步到后端配置

以下脚本从项目根目录运行，要求已在 `py/` 执行过 `uv sync --locked`。
它只同步存储连接配置，保留现有 `py/.env` 中的模型、token 等其他配置，不输出凭据。
已有部署请在确认当前密码或完成计划内轮换后运行；不要用示例文件覆盖已有 `.env`。
数据库 URL 由 SQLAlchemy 正确转义用户名和密码中的 `@`、`:`、`/`、`%` 等字符，
原始密码不要自行再次进行 URL 编码。脚本对应当前 Compose 的用户 `keel` 和库 `keel_learning`。
后端 dotenv 会展开 `${...}`；脚本会拒绝含这种语法的原样传递凭据，
避免悄悄改变密码。此类已有凭据应通过进程环境直接提供，或在计划内轮换后同步。

```powershell
@'
from pathlib import Path
from shutil import copyfile
from dotenv import dotenv_values, set_key
from sqlalchemy.engine import URL

root = Path.cwd()
source = dotenv_values(root / "infra/.env", interpolate=False)
required = ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "NEO4J_PASSWORD",
            "QDRANT_API_KEY", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
missing = [name for name in required if not (source.get(name) or "").strip()]
if missing:
    raise SystemExit("Fill infra/.env first: " + ", ".join(missing))
raw_keys = ("NEO4J_PASSWORD", "QDRANT_API_KEY", "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD", "MINIO_BUCKET")
if any("${" in (source.get(name) or "") for name in raw_keys):
    raise SystemExit("Secret interpolation syntax is unsupported; use process environment or rotate first.")
target = root / "py/.env"
if not target.exists():
    copyfile(root / "py/.env.example", target)
values = {
    "DATABASE_URL": URL.create(
        "mysql+pymysql", username="keel", password=source["MYSQL_PASSWORD"],
        host="127.0.0.1", port=3306, database="keel_learning",
    ).render_as_string(hide_password=False),
    "NEO4J_URI": "bolt://127.0.0.1:7687",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": source["NEO4J_PASSWORD"],
    "QDRANT_URL": "http://127.0.0.1:6333",
    "QDRANT_API_KEY": source["QDRANT_API_KEY"],
    "LEARNING_RAW_STORAGE": "minio",
    "MINIO_ENDPOINT": "127.0.0.1:9000",
    "MINIO_ACCESS_KEY": source["MINIO_ROOT_USER"],
    "MINIO_SECRET_KEY": source["MINIO_ROOT_PASSWORD"],
    "MINIO_BUCKET": source.get("MINIO_BUCKET") or "knowpath-materials",
    "MINIO_SECURE": "false",
}
for name, value in values.items():
    set_key(str(target), name, value, quote_mode="always")
print("Storage settings synchronized; no credentials printed.")
'@ | & .\py\.venv\Scripts\python.exe -
```

如果使用自定义主机端口或容器内运行后端，请按实际地址调整同步脚本；
进程中已有的同名环境变量优先于 `.env`，需要一并更新。使用 `config --quiet` 做校验，
不要把普通 `docker compose config` 的完整展开内容粘贴到日志或工单（其中包含凭据）。
之后重启 API、graph worker 和 model worker，确认数据库连接、图谱准备和向量查询正常。
`DATABASE_URL` 与 `NEO4J_PASSWORD` 缺失时客户端会明确报错；显式 SQLite URL 仍可用于隔离测试。

## 后端数据库迁移

进入 `py/`，在 `.env` 配置连接地址和密码，然后运行：

```powershell
uv sync --locked
uv run alembic upgrade head
uv run alembic current
```

Alembic 与后端使用同一份 `py/.env`，进程环境变量优先。正常安装和升级使用 Alembic；
目前迁移头为 `0015_material_object_storage`。SQL 模式要求表已建立，不在服务启动时自动建表。
备用 SQL 快照只适用于空库，详见 [迁移说明](../py/migrations/README.md)。

MySQL 已持久化学习空间、画像、评估、证据、掌握度、计划、会话、消息和导出等业务数据。
新上传原文件保存在 MinIO；SQL 保存对象引用、来源和导出内容，兼容旧 SQL 原文件。Neo4j 和 Qdrant 由 graph worker 按版本准备，
全部就绪且审核通过后才能发布；当前 HTTP 图谱查询读取 MySQL 保存的版本快照。
单独启动 MySQL 可用于部分 SQL 排查，但不足以完成完整的资料发布与向量问答。

正常运行还需启动 API、graph worker 和 model worker，命令见
[后端 README](../py/README.md)。默认使用单个 API 进程；持久任务通过 outbox 和租约恢复，
没有可恢复任务支撑的旧 Run 会在启动对账时终止，并非所有未完成任务都会直接失败。
修改 `.env` 后需重启相应进程。

已有数据库迁移前先备份。由旧 `init_db()` 建表而没有 Alembic 版本记录的库，需先比较实际
结构与迁移，确认一致后登记对应版本；不要直接对未知结构执行 `stamp head`。

## MinIO 文档存储

本项目从官方固定提交构建社区版镜像 `knowpath/minio:2025-10-15`：服务端
`9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`（RELEASE.2025-10-15T17-29-55Z），客户端
`d6541ea280b73a834b64d4097e21f2be77676104`。官方社区仓库现已停止维护、改为仅源码分发，
因此不依赖旧二进制镜像；固定版本不等同于持续获得安全更新。源码和许可证位于
[MinIO 官方仓库](https://github.com/minio/minio)，构建文件为 [minio/Dockerfile](minio/Dockerfile)。
第一次构建需要访问 GitHub、Docker Hub 和 Go 模块代理，耗时明显长于普通容器启动。

- API：`http://127.0.0.1:9000`，控制台：`http://127.0.0.1:9001`。
- 默认桶：`knowpath-materials`；对象键：`materials/{version_id}/raw`，原始文件名仅在 SQL 保存。
- `minio-init` 等待健康检查后创建私有桶；成功时状态为 Exited (0)，重复执行安全。
- 数据保存在 Compose 命名卷 `keel_minio`（当前 infra 组实际为 `infra_keel_minio`）。不要执行 `down -v`。
- 本地开发的 API 和 worker 使用 `py/.env` 中 `LEARNING_RAW_STORAGE=minio`，
  `MINIO_ENDPOINT=127.0.0.1:9000`、`MINIO_SECURE=false`，访问凭据与 `infra/.env` 对齐。
  容器内客户端地址应使用 `minio:9000`。部署到其他主机时按实际 TLS 配置调整。
- 未配置 `LEARNING_RAW_STORAGE` 的已有部署继续使用 SQL；配置为 MinIO 后仍可读取未迁移的 SQL 旧文件。

只向现有 infra 容器组新增服务：

```powershell
docker compose -p infra -f .\infra\docker-compose.yml up -d --build minio minio-init
docker compose -p infra -f .\infra\docker-compose.yml ps -a
```

若本机需要代理，构建时给 Docker CLI 设置 HTTP_PROXY/HTTPS_PROXY，给容器传入能访问宿主机的代理：
```powershell
docker compose -f .\infra\docker-compose.yml build --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 minio
```
代理地址仅为示例，不写入镜像运行配置。

旧文件迁移、失败处理和备份恢复见 [后端说明](../py/README.md#原始文档与旧文件迁移)。
