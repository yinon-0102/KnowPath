# Local Persistence Services

从 `KnowPath/` 根目录执行：

```powershell
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

端口已被占用时，可修改 Compose 映射左侧的宿主机端口，并同步 `py/.env` 的连接配置。
不要通过删除 Docker 卷解决端口冲突；卷中保存已有数据。

进入 `py/`，在 `.env` 配置连接地址和密码，然后运行：

```powershell
uv sync --locked
uv run alembic upgrade head
uv run alembic current
```

Alembic 与后端使用同一份 `py/.env`，进程环境变量优先。正常安装和升级使用 Alembic；
目前迁移头为 `0013_material_raw`。SQL 模式要求表已建立，不在服务启动时自动建表。
备用 SQL 快照只适用于空库，详见 [迁移说明](../py/migrations/README.md)。

MySQL 已持久化学习空间、画像、评估、证据、掌握度、计划、会话、消息和导出等业务数据。
原始上传与导出内容也保存在 SQL 中。Neo4j 和 Qdrant 由 graph worker 按版本准备，
全部就绪且审核通过后才能发布；当前 HTTP 图谱查询读取 MySQL 保存的版本快照。
单独启动 MySQL 可用于部分 SQL 排查，但不足以完成完整的资料发布与向量问答。

正常运行还需启动 API、graph worker 和 model worker，命令见
[后端 README](../py/README.md)。默认使用单个 API 进程；持久任务通过 outbox 和租约恢复，
没有可恢复任务支撑的旧 Run 会在启动对账时终止，并非所有未完成任务都会直接失败。
修改 `.env` 后需重启相应进程。

已有数据库迁移前先备份。由旧 `init_db()` 建表而没有 Alembic 版本记录的库，需先比较实际
结构与迁移，确认一致后登记对应版本；不要直接对未知结构执行 `stamp head`。
