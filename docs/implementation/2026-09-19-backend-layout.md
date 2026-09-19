# Backend Layout Refactor

Approved scope: organize the Python backend without changing HTTP behavior,
database schema, algorithms, environment keys, or Agent foundation packages.
The frontend is outside this change.

## Commit Sequence

1. Record the isolated test baseline, OpenAPI contract and database schema.
2. Split HTTP assembly, middleware, errors, dependencies and domain routers.
3. Group retrieval/indexing under `rag` and model adapters under `providers`.
4. Group SQLAlchemy metadata, repositories and shared UnitOfWork under
   `persistence`; explicitly name the material repository.
5. Group business modules and schemas under `materials`, `spaces`,
   `assessments`, `plans`, `conversations`, `knowledge`; group background
   execution under `workers` without redesigning mixed-responsibility services.
6. Update operational documentation, imports and package checks, run isolated
   regression and acceptance, then integrate and push incremental commits.

## Compatibility Requirements

- Keep `knowpath_backend.learning.main:app`, `learning.api.create_app`,
  `learning.graph_worker_cli`, `learning.model_worker_cli`, `learning.db_cli`,
  and `learning.vector_indexing` entrypoints.
- Preserve public exports of `learning.__init__`.
- Preserve all 49 endpoint signatures, operation IDs, authentication, error
  envelopes, idempotency rules, background dispatch and SSE behavior.
- Every APIRouter uses ContractRoute. Request dependencies reuse per-app
  services and the shared UnitOfWork.
- Keep a single SQLAlchemy Base and the existing migration revision chain.
- Keep config.py in place to preserve local-token path resolution.
- Keep state.py as the existing application facade and composition layer.
- Keep the test root and all Agent foundation packages in place.
- Preserve lazy imports that break provider/retrieval dependency cycles.

## Verification

Use the existing Python environment with the worktree Python root explicitly
on PYTHONPATH. Disable dotenv and model credentials for ordinary regression;
use memory repositories and disposable SQLite files. Compare normalized
OpenAPI and MySQL DDL to fixtures captured before moving modules. Exercise
migrations and package entrypoints. Live acceptance, when available, uses
only the existing script's disposable labeled Docker resources and cleanup.
Never migrate, clear or test against daily databases or Docker volumes.

Each implementation commit records its validation. Finish with a full suite,
independent review, and an ordinary fast-forward integration/push without
rewriting history.

Baseline at `4d54204`: 1361 passed, 217 skipped (207.61 seconds). Existing
Starlette/httpx deprecation warning retained. Frozen contracts contain 49 HTTP
operations and 25 application tables; Alembic adds its revision tracking table.
