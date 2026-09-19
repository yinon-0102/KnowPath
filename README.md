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

The backend project and its virtual environment are both under `py/`. The Python
package is `knowpath_backend`, distributed as `knowpath-backend`. It provides a
Web API and two background workers, alongside the reusable Agent foundation
(context, layered memory, tools, planning and verification). Only the interactive
terminal UI and its `knowpath` chat command have been removed.

```powershell
cd C:\Users\27202\Desktop\KnowPath\py
uv sync --locked
```

For the complete persistent learning backend, copy `py/.env.example` to
`py/.env` if it does not already exist, set `LEARNING_PERSISTENCE=sql`, and
configure the database connection and DashScope key. The template defaults to
`LEARNING_PERSISTENCE=sql` and `LEARNING_RETRIEVAL_BACKEND=qdrant`;
`keyword` is an optional retrieval fallback. Keep existing local settings.
After starting Docker, apply migrations from `py/`. Alembic reads the `.env`
beside `alembic.ini`; process environment variables take precedence:

```powershell
uv run alembic upgrade head
```

Run these three processes in separate terminals, all from `py/`:

```powershell
uv run python -m uvicorn knowpath_backend.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m knowpath_backend.learning.graph_worker_cli
uv run python -m knowpath_backend.learning.model_worker_cli
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
$env:PYTHON_DOTENV_DISABLED = "1"
uv run python -m pytest .\knowpath_backend\test -q
Remove-Item Env:PYTHON_DOTENV_DISABLED
```

The test directory covers the Web services and retained Agent foundation. Tests requiring
explicitly configured external databases skip when their `LEARNING_TEST_*`
variables are absent; only point these variables at disposable test stores.
Contract tests use deterministic model adapters;
they do not certify a paid provider's live availability or answer quality.

## PyCharm Startup (Windows)

Open the whole `KnowPath/` directory in PyCharm. Shared run configurations are
stored in `.run/`; select **KnowPath Backend** to start the API, graph worker and
model worker together. Each can also be started or debugged separately. The
configurations use `py/.venv/Scripts/python.exe`, `py/` as their working directory,
and SQL persistence. Run `uv sync --locked` from `py/` first when setting up a
new checkout. On another operating system, change the interpreter path in each
configuration to that system's virtual environment executable.

Start the Docker services below and apply `uv run alembic upgrade head` before
running the backend. Configure database connections and model credentials in
`py/.env`; all three processes load it automatically. No credentials belong in
the shared run configurations. This compound configuration starts the three
backend processes concurrently; it does not start Docker, apply migrations or
launch a frontend. `frontend/` is currently reserved. When stopping the backend,
stop all three processes in PyCharm's Run/Services window.

The API listens on `http://127.0.0.1:8000`; its basic health endpoint is
`http://127.0.0.1:8000/api/v1/health`.

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

The complete pre-cleanup source, including the removed terminal UI, is preserved
locally on `codex/archive-agent-before-web-cleanup-20260919` at commit
`0e9ca632155b34ec0e9ddb194b5a1b92791aa6e3`. The Agent foundation is restored in
the working tree; current Web requests still use the learning-domain pipeline.
Original attribution remains in [LICENSE](py/LICENSE).
