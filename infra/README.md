# Local persistence services

在项目根目录启动本地服务：

```powershell
docker compose -f infra/docker-compose.yml up -d
```

仅验证当前 SQL 后端时可以先只启动 MySQL：

```powershell
docker compose -f infra/docker-compose.yml up -d mysql
```

MySQL 默认映射宿主机 3306 端口；启动前需确保该端口可用。容器名中的 `-1` 是 Compose 实例编号。

进入 `py/`，配置并迁移数据库：

```powershell
$env:DATABASE_URL = "mysql+pymysql://keel:keel@127.0.0.1:3306/keel_learning"
uv run alembic upgrade head
uv run alembic current
$env:LEARNING_PERSISTENCE = "sql"
uv run uvicorn my_agent_llms.learning.main:app --workers 1
```

`0001_learning_schema` 建立业务基线表；`0002_run_events` 增加 Run 事件表及时间索引。迁移历史使用固定 DDL，不随当前 ORM 自动变化。SQL 启动要求迁移已应用，不再自动建表。

已有 `alembic_version=0001_learning_schema` 的数据库直接执行 `upgrade head`。若旧库由 `init_db()` 建立且没有版本记录，应先备份并比较其结构与对应迁移，确认一致后才登记该版本；不要直接对未知结构执行 `stamp head`。迁移测试覆盖空库、基线升级、数据保留与回退。

当前应用持久化资料及 Run/事件，其余学习业务数据仍在内存中；Neo4j、Qdrant 的容器定义不代表业务适配已全部接入。SQL 模式限单进程，启动时把未完成 Run 标记为中断失败。完整运行边界见 `../py/README.md` 的 Learning backend 部分。
