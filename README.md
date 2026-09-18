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

For the complete persistent learning backend, copy `py/.env.example` to
`py/.env` if it does not already exist, set `LEARNING_PERSISTENCE=sql`, and
configure the database connection and DashScope key. Set
`LEARNING_RETRIEVAL_BACKEND=qdrant` to use vector retrieval; the example
`keyword` setting is a local fallback. Keep existing local settings.
Apply migrations from `py/`:

```powershell
uv run alembic upgrade head
```

Run these three processes in separate terminals, all from `py/`:

```powershell
uv run uvicorn my_agent_llms.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m my_agent_llms.learning.graph_worker_cli
uv run python -m my_agent_llms.learning.model_worker_cli
```

The graph worker handles parsing, Neo4j/Qdrant preparation, corrections and
external material cleanup. The model worker handles durable assessment/message
generation and bounded retries. Uploaded knowledge must be reviewed and published
before a new learning space can bind it.

The API loads `py/.env`. Set `LEARNING_LOCAL_TOKEN` or let the API create
`py/.learning-token.local`; clients send the token in `X-Local-Token`. The token
file is ignored by Git. Browser origins must match `LEARNING_ALLOWED_ORIGINS`.
Unauthenticated `/api/v1/health` exposes only basic status; authenticated health
reports dependency availability without returning connection strings or keys.
The default embedding model is DashScope `text-embedding-v3`, 1024 dimensions.

PowerShell learning-service regression command (no wildcard script expansion):

```powershell
$learningTests = Get-ChildItem .\my_agent_llms\test\test_learning_*.py | ForEach-Object { $_.FullName }
uv run python -m pytest @learningTests -q
```

Tests requiring explicitly configured external databases skip when their test
connection variables are absent. Contract tests use deterministic model adapters;
they do not certify a paid provider's live availability or answer quality.

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
