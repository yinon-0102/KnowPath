# KnowPath

KnowPath is a learning application whose project root contains the Python
backend, frontend workspace, documentation, and local infrastructure.

## Project Layout

```text
KnowPath/
├── frontend/       # Frontend application (reserved)
├── py/             # Python backend and its uv environment
├── docs/           # Architecture and API contracts
└── infra/          # Docker Compose services
```

## Python Backend

The backend project and its virtual environment are both under `py/`:

```powershell
cd C:\Users\27202\Desktop\KnowPath\py
uv sync
uv run pytest -q
```

Run the learning API locally with:

```powershell
uv run uvicorn my_agent_llms.learning.main:app --reload
```

When persistence is enabled, the backend uses the SQLAlchemy/Alembic setup in
`py/` and the database services defined in `infra/docker-compose.yml`.

## Local Services

From the project root:

```powershell
docker compose -f .\infra\docker-compose.yml up -d
```

The Compose file provides MySQL, Neo4j, and Qdrant. If a host port is already
occupied, change only the host side of the mapping and update the backend
connection setting accordingly.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API contract](docs/API.md)
- [Backend README](py/README.md)
- [Infrastructure README](infra/README.md)
