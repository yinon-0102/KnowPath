# Durable graph worker and reviewed publication

## Execution design

This continues the approved GraphService/outbox architecture after 0010 staging. Migration 0011 adds optional preparation/publication JSON to graph revisions and durable lease token, lease expiry, availability and attempt count to outbox events.

- Run the SQL worker separately from the API. It consumes graph.prepare; API startup leaves graph_reconcile Runs for that worker instead of marking them interrupted.
- All command and worker mutations lock material, revision, outbox, then Run. A claim increments attempts and receives a new token; only an unexpired matching token may heartbeat or commit results. Expired claims can be taken over after process loss. Retries use bounded exponential delay and five attempts by default; terminal errors contain only a fixed safe error code/message.
- External operations run outside SQL transactions. Heartbeats separate Neo4j and bounded vector batches. Cancellation and material deletion revoke publication eligibility even if an external operation finishes late. Partial external writes remain rebuildable and inaccessible through source whitelists; physical garbage collection remains separate work.
- The immutable preparation manifest contains the candidate and the old side of each content-change conflict. Neo4j stores revision-scoped topic variants, exact source references and SUPPORTED_BY relationships with uniqueness constraints and transactional writes. Readback checks all stored rows and relationships. Qdrant embeds and indexes exact source bodies using the existing DashScope text-embedding-v3 / 1024 profile, then verifies every expected point.
- A receipt is bound to the complete manifest hash. Candidate pending_review, receipt, completed outbox and succeeded Run commit together. Neither a failed index nor a stale worker can make a candidate ready.

## Publication and bindings

Publication checks the expected current version and candidate base while holding the material lock. All review conflicts require explicit decisions; missing decisions return GRAPH_CONFLICTS_PENDING. keep_old/use_new require reasons. keep_both preserves both source sets and excludes the conflicted topic from automatic questions. Current extraction groups whole source-backed topics, so exclusion is conservative at topic granularity, not finer semantic assertion granularity.

The worker prepares both sides in advance. SQL publication derives a selected effective snapshot from that prepared superset, records decisions/timestamp, supersedes the old revision and assigns the next graph version in one transaction with the idempotent response. The original candidate remains immutable. The SQL snapshot governs visibility; the Neo4j projection includes variant data that must never be treated as independently published.

Existing spaces remain pinned. New spaces choose the latest published revision and persist graph_revision_id alongside material/version/graph-version. A superseded revision is still valid for existing bindings. Legacy spaces without graph_revision_id continue their previous parsed-version projection; provisional graph_version=1 never silently becomes the first formal revision. Message sources follow the topic snapshot, including older material versions retained by keep_old/keep_both. Source selection still honors exact space scope.

## Operation

From py/:

    uv run alembic upgrade head
    uv run python -m my_agent_llms.learning.graph_worker_cli

Use --once for one eligible claim. The printed job_claimed flag means a task was attempted; inspect its Run to distinguish success, retry, cancellation and terminal failure. Configure database, Neo4j, Qdrant and DashScope credentials in local environment or ignored py/.env. The worker does not auto-load example credentials. Run the API with SQL persistence to share durable jobs; an in-memory API cannot share jobs with this separate SQL worker.

No semantic relationship inference, upload-to-graph orchestration, correction confirmation, update adoption or physical graph/vector garbage collection is added by this module. Material deletion confirmation/version/cascade contract remains unfinished. APIs reading provisional material topic trees also need migration to the graph snapshot model; scoped space readers use pinned snapshots now.

## Validation

Tests exercise memory, SQLite and real MySQL claim races, lease expiry/restart, cancellation/deletion during I/O, retry exhaustion, transaction rollback, idempotent publication, conflict choices, old-space isolation and startup recovery. Real Neo4j and Qdrant integration uses deterministic test embeddings rather than paid provider calls. Full regression with all three actual stores enabled: 532 passed, 24 skipped, one pre-existing Starlette/httpx deprecation warning. Independent review found no confirmed actionable correctness findings. Local MySQL upgraded to 0011_graph_preparation.
