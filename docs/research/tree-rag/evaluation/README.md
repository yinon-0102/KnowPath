# First-batch controlled evaluation fixtures

These are **10 synthetic questions**, not the planned approximately 60 real-material,
manually reviewed evaluation questions. Every row has `source_kind: controlled`,
`synthetic: true`, and `human_review_status: pending`. No model experiment, human
review, quality improvement, or default-plugin selection is established by these files.
The real-material development set and holdout remain **pending**.

`dataset.jsonl` contains questions, permitted and excluded source spans, necessary
evidence, required answer points, answerability, and expected behavior. `split.json`
keeps all paraphrases and material families together: seven questions in five dev
families; three questions in three separate synthetic heldout families. Even a perfect
result on this synthetic heldout validates only the harness, never final RAG quality.
The heldout fixtures are public and agent-authored; they are not a blind test.

`sources/` contains deliberately invented material. Page boundaries are the page
sections recorded in `build_fixtures.py`; these fixtures do not test PDF parsing.
`service_failure_fixtures.json` specifies three controlled fault injections. It is
not a record of executed failures or a completed runner implementation.

## Rebuild and check

From this directory with Python 3.10 or newer:

```powershell
python build_fixtures.py
python build_fixtures.py --check
```

The first command regenerates only the fixture files owned by this script. The second
is read-only: it checks deterministic output, family isolation, exact artifact hashes,
source offsets and quotations, necessary evidence containment, excluded scope,
mixed-candidate rejection expectations, answer-point evidence links, and fault case
references. It does not run retrieval, models, scoring, or semantic human review.

Span coordinates are zero-based, half-open `[start, end)` **Unicode code-point**
offsets in the full material artifact, including page headings and newline characters.
`artifact_hash` is the lowercase SHA-256 hex digest of exact UTF-8 artifact bytes;
artifacts use LF line endings without a BOM. `material_version_id`, `artifact_hash`,
`page`, and nullable `block` must be preserved when resolving a reference. A span
quote is an audit copy; the immutable artifact remains authoritative. These ASCII
fixtures alone do not test multibyte normalization or UTF-16 offset conversions.

The excluded appendix is intentionally present on disk for scope-boundary testing.
Only the permitted spans may enter generation. The mixed public/restricted candidate
must be rejected whole; the allowed answer uses a separate public-only candidate.
Do not silently clip a mixed candidate and report that the scope test passed.

## Adding real evaluation data

1. Collect material with stable versions and parser artifact hashes; obtain permitted
   scope and preserve the original text needed to audit evidence coordinates.
2. Group shared source scenarios, near-duplicates, and paraphrases into families before
   splitting. Keep each family entirely in dev or heldout; use source-disjoint groups
   where feasible and record any shared-material contamination risk.
3. Annotate necessary evidence, conditions and exceptions, answer points, and honest
   answerability. Record reviewer identity and review outcome before changing a row
   from `pending` to a reviewed state. Agent-generated labels are not human review.
4. Keep offline gold points, forbidden-claim lists, expected statuses, and fault
   expectations out of online prompts and retrieval indexes. Runtime input contains
   the question, conversation history, source material, and permitted scope only.
5. Tune on dev and freeze inputs, rubric, model/prompt/profile settings, repeat count,
   statistics method, and measured numerical cost/latency gates before running the
   actual heldout selection experiment. See `rubric.md` for the required freeze fields.
6. Preserve all requests, failures, retries, paired results, usage, and timing. Publish
   separate harness, service integration, and real-model quality evidence. These
   controlled fixtures satisfy only the initial data-scaffolding part of that work.

Any source edit requires reviewed version/hash and coordinate regeneration. Do not
reuse a frozen real evaluation artifact after silently changing its text or labels.
No `freeze.json` or numerical deployment budget is supplied here: development
measurements and deployment requirements have not yet established them.
