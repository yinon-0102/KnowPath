# Backend Integration Acceptance

Date: 2026-09-19. Final live run started at 11:04:22 UTC (19:04:22 Asia/Shanghai).

The three approved backend stages are complete. Frontend was excluded.

## Changes

- `6d3fadd`: renamed backend package, restored all nonterminal Agent foundation,
  extracted reusable runtime/permissions, fixed Windows paths and storage closure.
- `9461eb5`: startup configurations and staged integration documentation.
- `816cb9c`: Web input budgeting/deduplication, extractive summaries and durable
  scoped recall from completed SQL messages, with version/retry/deletion fences.
- Acceptance runner: `py/scripts/accept_learning_backend.py --live`.

## Verification

- Full isolated suite: **1359 passed, 217 skipped**, one Starlette/httpx
  deprecation warning. Skips include opt-in external integrations and Windows
  symlink privileges; this does not mean all external tests ran.
- Final cleanup guards: **2 passed**. These run without Docker or paid calls.
- Two real synthetic workflows passed. The final run used DashScope `qwen-plus`
  and `text-embedding-v3`, MySQL 8.4, Neo4j 5.26 Community and Qdrant 1.13.6.
- Alembic created a fresh MySQL schema through `0013_material_raw`: **26 tables**
  including migration metadata.
- A synthetic functions document was uploaded and parsed; actual embeddings
  were indexed in Qdrant; a Neo4j topic variant was written and read back; its
  prepared graph revision was explicitly published.
- A learning space and scope were created; five actual model-generated
  diagnostic questions were answered and graded; a plan/session was started.
- A real grounded answer produced citations and six persisted SSE events.
  `Last-Event-ID` resumed with only the remaining terminal event.
- A durable message was queued, fresh application/services reconstructed, startup
  reconciliation performed, and a worker completed the original Run with scoped
  historical recall. Its measured input remained within the configured budget.
- Five actual model-generated retest questions were answered and graded. Fresh
  services read back **five evidence rows belonging to that retest**.

## Isolation And Limits

Each run created three uniquely named, labeled disposable containers, random
loopback ports and independent storage. Only model configuration/keys were read
from local `.env`; daily database/container settings were overridden. All three
temporary containers were removed after each run, with no remaining owned
containers reported. No daily database migrations or data changes were made.

The JSON report is written to `%TEMP%/knowpath-live-acceptance.json`; it contains
check names, counts and model names, without keys or connection credentials.

HTTP routes were exercised through FastAPI TestClient, not a browser, reverse
proxy or separately launched ASGI network server. Recovery reconstructs services
against durable stores; lease takeover/cancellation are covered by isolated
regressions, not an OS-process crash in this live script.

Graph extraction uses the existing deterministic parser and published SQL
snapshots; this is not an LLM graph-extraction quality evaluation or proof of
Neo4j-backed online graph traversal. Generated questions/answers passed existing
structure and citation validation, not an expert semantic-quality benchmark.

Conversation summaries are extractive; recall uses a per-request TF-IDF index
rebuilt from SQL. Token budgets use an estimator, not DashScope's exact tokenizer.
No automatic L0 fact promotion or unrestricted Agent file/Shell tools are exposed
through Web conversations. These limits preserve the agreed domain boundaries.
