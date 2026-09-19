# Database migrations

The schema is defined in `knowpath_backend.learning.db`. Set `DATABASE_URL` to a
MySQL connection string, then run:

```powershell
uv run alembic upgrade head
```

For a local schema smoke test without MySQL, `init_db(create_db_engine(DatabaseSettings("sqlite+pysqlite:///keel_learning.db")))`
creates the same tables in SQLite.

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
