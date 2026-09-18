# Local persistence services

Start MySQL, Neo4j and Qdrant from the repository root with:

```powershell
docker compose -f infra/docker-compose.yml up -d
```

Then, from `py/`, apply the SQL schema:

```powershell
uv run alembic upgrade head
```

The FastAPI local slice uses in-memory repositories by default. Set
`DATABASE_URL`, `NEO4J_URI` and `QDRANT_URL` before wiring persistent adapters.
