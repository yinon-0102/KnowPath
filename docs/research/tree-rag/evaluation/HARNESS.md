# Paired evaluation harness

This harness runs frozen A/B1 requests and reports external human judgments. The
existing ten synthetic questions remain unreviewed controlled fixtures. No quality
improvement, deployment budget, or plugin adoption is established by harness tests.

From `py/`, use:

```powershell
python -m knowpath_backend.rag_eval.cli freeze --config ../docs/research/tree-rag/evaluation/development.example.json --output experiment.freeze.json
python -m knowpath_backend.rag_eval.cli run --freeze experiment.freeze.json --partition dev --output experiment.results.jsonl
python -m knowpath_backend.rag_eval.cli report --freeze experiment.freeze.json --results experiment.results.jsonl --reviews human-reviews.json --output experiment.report.json
```

Copy and complete the example first. Its empty runtime mapping intentionally makes
`run` refuse execution. Real requests need the material uploaded, its space scope
set, and a common B1-ready manifest published for both modes. The synthetic scope
IDs in the dataset do not identify actual application spaces.

`runtime_bindings` maps a question ID (takes precedence) or dataset scope ID to:

```json
{
  "space_id": "actual-space-id",
  "scope_snapshot_id": "actual-current-snapshot-id",
  "expected_scope_version": 1,
  "expected_bindings": [{"material_id": "actual-material-id", "material_version_id": "actual-version-id", "graph_version": 1}],
  "expected_manifest_ids": ["actual-manifest-id"],
  "expected_retrieval_versions": ["actual-retrieval-version-id"],
  "manifest_configuration_hashes": {"actual-manifest-id": "sha256-of-canonical-manifest-configuration"}
}
```

Use `knowpath_backend.rag_eval.dataset.digest(manifest["configuration"])` to compute
configuration hashes. `profile.manifest_configuration_hashes` must contain the union
of those mappings. Runtime scopes, bindings, manifest IDs, retrieval versions and
configuration hashes are checked before and after every request. Response traces
must confirm the same IDs. A change records a failed attempt; it never silently
switches indexes. Only question, history role/content, and explicit runtime scope/binding arguments reach
the pipeline. Gold evidence, required points, exclusions and expected status remain
offline. Conversation history passes through the pipeline's shared conservative
query preparation; no gold evidence or rewrite is injected by the harness.

`freeze` hashes exact config, dataset, split, rubric and all referenced source bytes,
plus all learning-module and harness Python files (including prompts, persistence,
scope and publication guards), and the available project dependency manifest/lock.
Source quotes and artifact hashes are checked, and family leakage across partitions
is rejected. Frozen files must remain in place and unchanged; copying or editing the
config after freeze requires a new freeze. This is an integrity record, not a signed
attestation. Real runs check frozen models/providers/endpoints, prompt implementation,
profile hashes, environment and budgets against the configured runtime. Currently
only the runtime's implemented budgets are supported; an unsupported value fails.
Credentials remain environment-only and should never appear in experiment files.

Every question has the same repeat count. The first pair runs A then B1 and the
next B1 then A, alternating across all question/repeat pairs. Every attempt is flushed to JSONL. Files are created
exclusively, with no overwrite or hidden resume/retry. An interrupted file can be
reported: missing scheduled rows remain in the full denominator as unknown. A new
experiment needs a new output filename, and rerunning only favorable questions is
not a supported operation. Tests can inject `pipeline_factory(mode)` and
`scope_resolver(space_id)` into `runner.run`; these results are labeled
`injected_harness`, never real-model evidence.

Human review is a JSON array, one record per question/plugin/repeat:

```json
[{"question_id":"ctrl-001","plugin":"a","repeat":0,"success":false,"partial":true,"error_type":"context"}]
```

Repeat indexes are zero based. `success` and `partial` are strict booleans and cannot
both be true. A response marked partial by the pipeline cannot receive full success.
Missing reviews leave quality unknown; model self-checks never supply these labels.
Service failures count as zero success and remain in all-request denominators.
Separate service-success denominators are reported. Repetitions are averaged within
question, then questions within family; families receive equal weight. Reports give
paired wins/losses/ties, family differences and their descriptive range. That range
is not a confidence interval, and this implementation makes no automatic adoption
decision. Category breakdowns overlap and are not independent samples.

Request latency includes the complete attempted call through terminal failure. P50
and P95 use nearest-rank quantiles. Cost is known only when the response trace has
`cost: {"complete": true, "amount": ..., "currency": ...}` for all request stages.
When complete actual usage and a frozen price table are available, the harness
can estimate request cost using `input_per_million` / `output_per_million` rates
and optional `per_call` fees. Aggregated embedding usage needs its physical HTTP
call count when a fixed fee applies; chat needs an explicit output-token price.
This estimate is not a supplier billing statement. Missing counters, rates, or a
failed request keep aggregate costs unknown; missing usage is never zero.
Build/update costs are separate from this request harness; new build attempts
persist call reservations and safe usage in their write-intent audit. Frozen pricing
does not invent unobserved tokens or costs. Numerical gates are evaluated only where measurements exist;
undefined ratios cannot pass.

Failed records retain `error_details` only when a trusted adapter provides them:
generation/verification stage, one-based call index, budget counters, validated
token counts, and allowlisted response/finish categories. Missing details remain
unknown. Provider text, prompts, URLs, request IDs, and arbitrary exception strings
are excluded. This does not reconstruct earlier failed runs or their missing bills.

Online generation/checking and this harness read the same explicit
`RAG_MODEL_INPUT_TOKENS` / `RAG_MODEL_OUTPUT_TOKENS` values (defaults 12000/2000).
Their positive integer sum must fit the configured context capacity. Changing them
requires a new freeze; no candidate/context budget or retry behavior changes implicitly.

Heldout execution requires `human_review_status: approved` on configuration, split
and every dataset row; all four finite positive numerical gates; and a frozen
currency, price date and price table. The example deliberately leaves these gates
null and approval pending. No elapsed time, development score or synthetic fixture
test substitutes for human review. Candidate, reranked, context and citation evidence coverage compare
the returned original source spans with necessary evidence entirely offline. Full
union coverage with identical material version, artifact hash, page and block is
required. Empty evidence sets and failed responses are unavailable, with explicit
coverage denominators. This geometric coverage does not establish semantic support.
Unsupported-claim and scope-leak counts require richer external annotations; this
version reports provided success/partial/error attribution rather than inventing
those metrics. Synthetic-to-real source identities must be annotated consistently
before their coordinate coverage can be interpreted.

The runtime can independently enforce pre-send monetary reservations when both
`RAG_MAX_REQUEST_COST` and `RAG_PRICING_FILE` are set. This configuration, including
the full price table, is frozen inside budgets. No configuration means an explicit
disabled monetary ceiling, not zero-price inference or passed cost acceptance.
See `../OPERATIONS.md` for the distinction between conservative reservations,
actual usage estimates, and supplier invoices.

To render the final real-material run for independent review, from `py/` use:

```powershell
python ../docs/research/tree-rag/evaluation/render_review.py --freeze experiment.freeze.json --results experiment.results.jsonl --output review-packet
```

The packet includes unfilled review entries and source-coverage diagnostics. Copy
only actually reviewed entries into the `--reviews` input; null does not mean
incorrect or reviewed. The renderer makes no provider calls and does not change
the frozen dataset or its approval state. Real-material label drafts live in
`real-draft/`, including their public-source checksums and explicit limitations.

The reliability repair freezes the complete compact wire configuration under
`prompts.protocol`: response format, canonical draft byte cap, generation/check
time reservations, evidence locator version, and counting profile provenance.
`segment-id-v1` supplies all original text as deterministic segments and lets the
checker select locally defined IDs. The server restores exact Unicode offsets;
models neither count character positions nor reproduce PDF whitespace.

`whole-source-v1` shows generation only full source text and source IDs; segment
IDs are confined to checking. `request-enum-v1` binds allowed source, segment and
claim IDs in the native schema through the same body builder used for counting
and sending. The bounded claim pool is c1 through c12. `unicode-plain-v1` changes
output notation instructions without modifying source text. A provider `stop`
finish reason does not make incomplete JSON valid. These policies are frozen.

Every ConfiguredPipeline request owns a bounded call journal. Successful rows
retain it under `response.trace.call_journal`; failed rows retain it under
`call_journal` independently of an answer. Each physical HTTP call reports stage,
safe failure/contract classification, duration and available provider usage.
Retrieval HTTP calls are distinguished from billed model calls. Missing usage
remains unknown; neither planned reservations nor JSON parsing success imply an
actual invoice or semantically correct answer. Labels and model/checker verdicts
remain separate. Interrupted experiments preserve the full scheduled denominator
and explicitly identify missing records and potentially in-flight unknown cost.
