# Backend Stabilization and Memory Integration

**Goal:** Complete the user's approved sequence: stabilize the restored backend,
integrate scoped context/memory into Web conversations, then validate a real
learning workflow on isolated storage. Frontend is explicitly excluded.

## Stage 1: Stable Foundation

- [x] Fix portable tool paths and SQLite connection lifecycle using failing
  regressions; use the active Python executable in subprocess tests and skip
  only real symlink tests when the platform denies creating links.
- [x] Run all isolated tests, review changes, and commit the restored package/UI
  separation and Windows fixes in logical modules. Do not commit secrets.

Validation: full isolated suite passed (1338 passed, 210 skipped; one upstream
Starlette deprecation warning). Review found relative-path SQLite close and an
inherited runtime memory-replacement leak; both reproduced before fixes and
passed with the 39-test focused post-review run. External integrations were
disabled in this stage; the two symlink privilege skips are explicit.

## Stage 2: Web Context and Memory

- [x] Inspect the existing ContextEngine/Memory interfaces and Web transactions;
  document a concrete adapter design with namespace and persistence boundaries.
- [ ] Add token budgeting/deduplication and bounded historical summaries to Web
  messages, preserving source citations and treating recalled text as data.
- [ ] Add durable scoped recall through existing SQL records or explicit schema
  migration, with provenance, space/version isolation, restart reconstruction,
  retry idempotency and deletion/late-worker protection.
- [ ] Verify focus scenarios and full regressions, document limitations, review
  and commit this integration. Keep grading/profile authority in domain services;
  do not expose arbitrary Agent Shell or file tools through Web requests.

## Stage 3: Real Workflow Acceptance

- [ ] Inspect available Docker/services and model configuration without exposing
  credentials; provision separate disposable stores and synthetic test material.
- [ ] Exercise upload, graph review/publication, space, diagnostic, plan, message,
  retest, SSE and persistence/recovery using actual model adapters and stores.
- [ ] Record results, clean only owned temporary resources, commit acceptance
  artifacts, and report any external blocker precisely. Never run regression
  fixtures or unrestricted workers against the user's daily database.

Existing user authorization covers all three stages; routine implementation
choices follow the current architecture. Live calls use synthetic material and
bounded request counts. No frontend or unrelated runtime redesign is included.

## Web Adapter Design

- Completed `learning_messages` are the durable memory log. A request rebuilds
  its recall index from messages with the same space, scope version and bindings.
  Pending, failed, cancelled and stale messages never enter memory. No extra
  local Agent database or schema migration is needed.
- Recent turns remain in conversation history. Older turns in that conversation
  produce bounded extractive summaries through `SummaryMemory`; relevant turns
  across conversations use the foundation's TF-IDF `InMemoryVectorBackend`.
  Each excerpt carries its original message/conversation IDs. These are historical
  context, never new learning evidence or authoritative learner-profile facts.
- `ContextEngine` selects and deduplicates optional context. Trusted instructions,
  the complete current question and at least one source are protected separately.
  The adapter measures the serialized final prompt using the existing estimator,
  caps sources if needed, and rejects an oversized mandatory prompt explicitly.
  This is an estimated input budget, not the provider tokenizer's exact count.
- Context freezes in the existing message snapshot and follows its transaction,
  worker lease, cancellation, version checks and idempotency lifecycle. Material
  erasure also clears derived memory fields; space erasure removes the log.
- Memory and summary text remain JSON data inside the user message, never a
  system-role instruction. Shell/file tools and automatic L0 promotion are not
  exposed by this adapter. Retrieval currently rebuilds per request; large-space
  indexed recall can be added when measurements justify a separate index.
