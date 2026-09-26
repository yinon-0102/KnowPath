# Local Persistence Services

从 `KnowPath/` 根目录执行：

```powershell
if (-not (Test-Path .\infra\.env)) { Copy-Item .\infra\.env.example .\infra\.env }
# 先在 infra/.env 填写随机 MinIO 密码，并同步到 py/.env。
docker compose -f .\infra\docker-compose.yml up -d
docker compose -f .\infra\docker-compose.yml ps
```

若当前在 `KnowPath/py/`，文件路径改为 `..\infra\docker-compose.yml`。
容器名中的 `-1` 是 Compose 实例编号；应确认状态为 running/Up，而非 created。

| 服务 | 默认宿主机端口 | 用途 |
| --- | --- | --- |
| MySQL 8.4 | 3306 | 业务数据、来源和图谱快照、Run/事件、outbox 任务 |
| Neo4j 5.26 | 7474、7687 | 按版本准备的知识图谱 |
| Qdrant | 6333、6334 | 按模型和资料版本隔离的向量索引 |
| MinIO | 9000、9001（仅本机） | 原始文档 S3 API、管理控制台 |

端口已被占用时，可修改 Compose 映射左侧的宿主机端口，并同步 `py/.env` 的连接配置。
不要通过删除 Docker 卷解决端口冲突；卷中保存已有数据。

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
