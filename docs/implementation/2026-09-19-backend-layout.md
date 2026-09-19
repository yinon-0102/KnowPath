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

## Completed Layout

All six stages are implemented. Public startup modules remain at their original
locations; their worker/indexing implementations are delegated to the new
packages. Agent foundation and frontend files have no changes.

| Previous location | Current location |
| --- | --- |
| `learning/api.py` | `learning/api/application.py` and `api/routers/` |
| `learning/http_contract.py` | `learning/api/contract.py` |
| `learning/vector_retrieval.py` | `learning/rag/retrieval.py` |
| `learning/vector_indexing.py` implementation | `learning/rag/indexing.py` |
| `learning/model_adapters.py` | `learning/providers/models.py` |
| `learning/db.py`, repositories and UnitOfWork | `learning/persistence/` |
| `learning/repositories.py` | `learning/persistence/material_repository.py` |
| Material upload, deletion, ingestion, sources | `learning/materials/` |
| Spaces, profiles, timeline, deletion, exports | `learning/spaces/` |
| Assessments, grading, questions, mastery | `learning/assessments/` |
| Planner and planner policy | `learning/plans/` |
| Messages, answer generation, conversation context | `learning/conversations/` |
| Graphs, corrections, knowledge updates | `learning/knowledge/` |
| Durable jobs, process implementations, Run events | `learning/workers/` |

Each domain's request schemas are colocated with its services. Mixed service and
worker files (for example material deletion) remain intact within their owning
domain. The legacy `indexes.py` adapters remain available separately from current
graph preparation and retrieval implementations. Historical implementation
records retain their original file references.

## Validation Results

- HTTP stage: 856 passed, 215 skipped; exact OpenAPI snapshot preserved.
- RAG/provider stage: 163 passed, 44 skipped; canonical exceptions and lazy
  adapter imports preserved. Public module startup checks passed separately.
- Persistence stage: 79 passed, 27 skipped; MySQL DDL snapshot and SQLite
  migration upgrade/downgrade/record-preservation checks passed.
- Final full suite: 1367 passed, 217 skipped in 170.78 seconds. The existing
  Starlette/httpx deprecation warning remains. Unconfigured external-store and
  platform-specific tests retain their existing skip conditions.
- All 48 relocated RAG/provider/persistence/business implementations passed an
  AST comparison with their previous versions after excluding import changes.
- The wheel built offline. Imports of all new domain packages, public worker
  entrypoints and Agent foundation were verified directly from the wheel, with
  the frozen 49-operation OpenAPI comparison passing.
- Independent reviews found no actionable regressions in HTTP, RAG/providers,
  persistence or business/worker relocation.
- Live acceptance used real `qwen-plus` and `text-embedding-v3` calls with three
  disposable Docker containers. Fresh MySQL migrations reached
  `0013_material_raw` with 26 tables including Alembic tracking; graph publication
  created a Neo4j topic and the real embedding index. Five-question diagnostic
  and retest grading, plan/session creation, grounded messages, SSE resumption,
  worker restart, scoped memory recall and retest evidence persistence passed.
  All three temporary containers were removed; no daily database was used.

No database schema or migration revision changes are required to run this
refactor. Existing PyCharm run configurations and environment keys are retained.
