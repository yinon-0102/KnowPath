# Durable graph reconciliation candidates

## Scope and contract

This module replaces the reconcile and graph-diff placeholders with persisted, source-backed candidate snapshots. It removes fabricated publication success. It is the staging portion of the approved GraphService/outbox architecture, not a completed graph publication pipeline.

- Reconcile requires an exact material version and a non-negative integer expected_graph_version. The base is the latest published revision for this material, or zero when no revision has been published.
- Candidate, queued Run, graph.prepare outbox event, and idempotent response commit together. The material row serializes sequence allocation and base selection. SQL deadlocks/duplicate idempotency inserts use the existing bounded command retries.
- Each candidate has a material-local monotonic sequence, a content-addressed immutable snapshot, an exact material-version reference and a stored diff. SQL persists these in 0010_graph_revisions; the in-memory adapter has the same rollback boundary.
- The current extractor projects parsed headings and chunks. It does not infer semantic relations. Source references/hashes are retained in the snapshot; full source text remains in source_chunks. Changes to the content of the same topic are conservatively marked content_change for review, not asserted to be semantic contradictions.
- Diff entries use kind=node/relation/source plus id, before, after. include_unchanged controls the unchanged array. Explicit revision IDs are checked against the requested material. No candidate returns a null candidate_revision_id and the actual published base (initially zero).
- Repeating the same idempotency key returns the original accepted response. Concurrent requests targeting the same material version/base also share an active draft and Run. Failed/cancelled Runs may be restaged with a new key. Created-at timestamp rounding does not decide which candidate is newest.
- Publish validates ownership, expected version and conflict-resolution shape. keep_old/use_new requires a non-empty reason. Until external preparation and atomic publication are implemented, publish returns REVISION_NOT_READY. It never returns a made-up graph version or uses a revision ID as a material-version ID.
- Existing material deletion now cancels graph Runs, clears candidate snapshots and their outbox events, and tombstones graph replay responses inside the deletion transaction. It still rejects materials referenced by a learning space; complete source cascade is a separate outstanding module.

## Compatibility and unfinished work

The existing parsed-material graph_version=1 projection used by learning spaces is unchanged and is not a published GraphService snapshot. Candidates do not change existing bindings or become available for automatic question generation. Initial GraphService reconcile therefore expects zero even when a legacy space uses graph_version=1.

There is currently no consumer for graph.prepare. These Runs remain queued until cancelled or interrupted by the existing single-process startup recovery. Startup recovery marks active Runs RUN_INTERRUPTED; it does not erase candidates or retry jobs. New keys can restage failed Runs. Do not expect graph reconciliation to complete automatically in this increment.

Next work: worker claim/lease/retry handling, validated graph extraction, Neo4j and Qdrant preparation receipts tied to snapshot_hash, conflict decisions and re-preparation, transactional publication pointer, then learning-space update discovery/adoption. Knowledge corrections and full material deletion must use those boundaries.

## Verification

Regression coverage includes memory, SQLite and real MySQL: restart reads, original-response replay, concurrent staging, injected transaction rollback, snapshot diff, strict HTTP validation, unprepared publication refusal and deletion tombstones. Migration tests compare the complete SQLAlchemy schema against Alembic head and exercise upgrade/downgrade paths. Tests do not call paid model APIs.

Validation after review: 491 passed, 24 skipped, 1 pre-existing Starlette/httpx deprecation warning in the full learning suite with actual MySQL and Qdrant enabled. Local MySQL is at 0010_graph_revisions (head); alembic check reports no new upgrade operations. Independent review reproduced and verified the source-order fix with SQLite reverse_unordered_selects; snapshot hashes and diffs remain identical.
