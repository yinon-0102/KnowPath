# Vector retrieval implementation

Continues the approved ARCHITECTURE.md section 7.1 / storage design.

## Scope
- DashScope text-embedding-v3, 1024 dimensions, strict response validation, bounded batches/timeouts, no redirects or upstream error disclosure.
- Qdrant adapter uses Cosine and a collection name scoped to embedding profile and index schema. Payload contains identifiers/hashes, never source text or assessment keys.
- Explicit index CLI reads an exact ready material version from SQL and upserts deterministic points. It does not publish graphs or migrate space bindings. Until graph/outbox ingestion exists, indexing remains an explicit preparation step.
- Read-only retrieval intersects results with the source snapshot for the space's pinned versions and selected topics. Content hashes and graph version are part of point identity. Missing/incomplete indexes fail explicitly; no silent keyword fallback.
- Message preparation snapshots all allowed candidates; external retrieval runs after commit. Publication rechecks scope, bindings, cancellation and deletion. Only selected references are emitted. Default keyword mode remains available while operators prepare indexes; qdrant mode is explicit.

## Work sequence
1. Provider and real local Qdrant tests (including malformed vectors, duplicate indexes, partial index, version/scope isolation and replay).
2. Embedding/index/retrieval adapters and explicit CLI.
3. Message retrieval injection, transactional publication and lifecycle tests.
4. CLI/config documentation, complete learning suite, optional real Qdrant/MySQL verification, independent review, module commit.

## Deliberate limits
Graph reconciliation/publication, durable outbox and automatic upload indexing remain separate work. This module must not claim them complete. No real paid API calls without a separately justified end-to-end run. Existing remote push rejection remains respected.

## Verification outcome
- Completed adapters, explicit version index CLI, opt-in Qdrant message retrieval and operator documentation.
- Full learning suite with actual MySQL and Qdrant enabled: 467 passed, 24 skipped, 1 existing Starlette/httpx warning. Provider boundary uses MockTransport; no paid embedding/chat calls.
- Independent review reproduced float32 overflow/underflow acceptance; fixed by target-range validation and stable normalization before index/query. Added regression coverage for both Qdrant local engine and server.
- Verified source selection after commit, immutable source identity, incomplete-index failure, cancellation/deletion/scope changes before answer generation, idempotent replay, more than eight candidate chunks, and owned-client shutdown.
- No SQL schema change is required. Graph/outbox automatic preparation and physical deletion cleanup remain outstanding; the maintenance CLI can leave unreachable orphan vectors if a source is concurrently deleted.
