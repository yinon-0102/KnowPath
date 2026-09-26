# Database migrations

The schema is defined in `knowpath_backend.learning.persistence.db`. From `KnowPath/py/`,
configure `DATABASE_URL` in `.env`, start MySQL, then run:

```powershell
uv run alembic upgrade head
uv run alembic current
```

Alembic loads `.env` beside `alembic.ini` without overriding process environment
variables. `PYTHON_DOTENV_DISABLED=1` disables this loading for isolated tests.
Normal initialization and upgrades use Alembic; the API does not create tables.
当前迁移头为 `0015_material_object_storage`。该迁移添加原始文档对象引用、允许原字节为空，
不自动搬运文件。数据迁移及回退要求见 [后端说明](../README.md#原始文档与旧文件迁移)。
Back up existing databases before upgrading. Regression tests in
`test_learning_migrations.py` use temporary SQLite databases to verify upgrades,
downgrades, schema parity, configuration precedence and data preservation.

## Initialize an empty MySQL database with SQL

`../init_mysql.sql` is a MySQL 8.4 schema snapshot at revision
`0013_material_raw`. It creates `keel_learning`, all 26 tables (including
`alembic_version`), indexes and foreign keys, and records the migration revision.
Open it in a MySQL client or database GUI and execute it against a new empty
database, stopping on the first error. To use another database name, edit both
`CREATE DATABASE` and `USE` in the file before execution.

Do not run this initialization script against an existing installation. Continue
using `uv run alembic upgrade head` for future upgrades. The SQL file contains no
business data, credentials, Neo4j nodes or Qdrant indexes.
