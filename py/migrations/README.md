# Database migrations

The schema is defined in `my_agent_llms.learning.db`. Set `DATABASE_URL` to a
MySQL connection string, then run:

```powershell
uv run alembic upgrade head
```

For a local schema smoke test without MySQL, `init_db(create_db_engine(DatabaseSettings("sqlite+pysqlite:///keel_learning.db")))`
creates the same tables in SQLite.
