# Evaluation rubric, draft v1

Status: controlled harness specification; human review pending. This rubric and the
synthetic fixtures do not establish a production quality baseline or complete the
planned real-material evaluation. Freeze the reviewed rubric before actual heldout
runs, and retain its content hash with each experiment.

## Primary outcome and supporting measurements

The primary outcome is **full task success per attempted request**. For an answerable
question, success requires all required answer points, each supported by admissible
evidence, with necessary conditions and exceptions preserved. Citations must resolve
to the correct material version and artifact, lie entirely in the permitted source
spans, and support the cited claim. Required points cannot be credited solely because
they sound correct or occur in model memory. Any material unsupported assertion,
forbidden claim, excluded-source disclosure, or unresolved citation error prevents
full success. A response marked `answered` is not itself proof of success.

The denominator includes every scheduled question/repeat/plugin request, including
provider failures, timeouts, invalid verification output, budget exhaustion, and
missing expected responses. Preserve the planned run matrix and reconcile missing
rows as failures; silently dropping a failed pair is not permitted. Record cancellations
and their cause explicitly under the frozen request policy. Retries consume the same
request's elapsed time and cost and do not erase its original attempt records.

Service failure has outcome `failed` and full-task score 0; it must not be relabeled
`insufficient`, an appropriate refusal, or an unanswerable gold question. A fault
injection may pass its separate engineering assertion by producing `failed`, while
still scoring 0 on the user-task metric. Verification failures must not publish an
unverified body. Report fault-injection experiments separately from quality runs.

Record three different fields: the expected/observed response status, a binary
`full_task_success`, and a separate `partial_progress` measure. On answerable items,
a `partial` response can earn supported-point coverage but cannot earn full success.
Report coverage as supported required points divided by required points; report
unsupported claims and citation failures separately. When no fact point is required,
coverage is not applicable rather than a vacuous 100%.

## Answerability-specific decisions

| Gold answerability | Required behavior | Full-task decision |
| --- | --- | --- |
| `answerable` | Answer all required points, cite permitted evidence, retain conditions and exceptions. | All required points and behavioral checks pass; no disqualifying claim. |
| `unanswerable_in_scope` | Return `insufficient`; state the scoped limitation without inventing a fact or revealing excluded content. | Correct limitation and all required behavior pass. A failed service never earns this credit. |
| `ambiguous` | Return `clarify` and ask a targeted question that distinguishes the actual plausible referents. | Clarification resolves the stated ambiguity without selecting an unsupported referent. |
| `conflicting` | Report both supported alternatives, disclose the unresolved conflict, and withhold an authoritative choice. Fixtures expect `partial`. | Complete the conflict-reporting task with all listed points and required behavior. An invented resolution fails. |

The conflict row measures success at faithfully reporting a conflict, not success at
recovering a definitive fact. Report conflict-task success separately from answerable
full-answer success and from partial progress. Do not promote ordinary partial answers
to full success because a conflict item legitimately expects a `partial` status.

An `insufficient` answer cannot assert that an entire source lacks a fact merely
because Top-K retrieval did not find it. Whole-artifact absence claims require the
corresponding coverage check. These tiny controlled absence fixtures provide the
complete allowed card/note; they do not justify the same inference over long material.

For condition and exception items, score the condition together with its dependent
conclusion. A correct number or date with the wrong applicability is unsupported for
the task. For scope tests, inspect admitted candidates and final context as well as
the answer; a lucky answer does not excuse admitting the excluded mixed candidate.

Offline graders may use the gold dataset. The online pipeline must not receive
`necessary_evidence`, gold answer points, expected statuses, forbidden claims, or
fault expectations. Human reviewers must audit important semantic judgments; this
scaffold records no completed review.

## Paired reporting and frozen statistics

Run A and B1 on the same frozen question/scope/material snapshots and repeat schedule.
Alternate execution order under a frozen schedule. Preserve both sides when either
fails. Do not repeat selectively after inspecting unfavorable outcomes. Aggregate
repetitions within a question before computing question-level paired effects; do not
treat repeated calls or paraphrases as independent questions.

Report full-task success for all requests, plus a clearly labeled service-success
subset with its own denominator. Also report answerability/category breakdowns,
partial progress, supported-point coverage, candidate/context evidence coverage,
unsupported claims, scope leaks, service failure rates, and paired wins/losses/ties.
Attribute errors to parsing, recall, reranking, context pruning, generation,
verification/citation, or service faults using retained trace evidence.

Select and freeze the statistical method on dev before actual heldout execution.
Family-paired bootstrap is one candidate, not a mandated method. The freeze must
record the chosen estimand, method, weighting, handling of failures, repeat aggregation,
confidence level, random seed, and resample count where applicable. Report differences
with uncertainty and the number of independent families. A small heldout supports
exploratory conclusions only. **There is no pre-agreed adoption rule requiring a
confidence-interval lower bound above zero.** Any adoption rule must be explicitly
recorded before heldout results are inspected; statistical uncertainty must remain
visible even when a practical choice is made.

## Numerical gates required before actual heldout selection

Measure latency from request start to final verified response or terminal failure,
including retrieval, generation, verification, repairs, and retries. Report P50/P95
with a frozen quantile convention, mean cost per attempted request, actual model
calls/tokens, and separate build/update costs. Failed calls contribute known cost;
unknown usage is flagged as missing, never silently zero. Such missing measurements
cannot establish that a cost gate passed.

The frozen configuration must contain numerical deployment-informed values for:

- `max_p95_latency_ms`
- `max_mean_cost_per_request` (with currency and frozen price table/date)
- `max_b1_latency_ratio` (B1 P95 / A P95)
- `max_b1_cost_ratio` (B1 mean request cost / A mean request cost)

No budget values are invented in this draft. Derive the values from development
measurements and deployment requirements. Undefined ratio denominators must be
reported as unavailable under a predeclared policy; they cannot automatically pass.
Missing/non-numeric gates block an actual heldout plugin-selection experiment. A
synthetic harness test may exercise rejection of a missing gate; its success does
not authorize real heldout execution or a deployment decision.

The eventual freeze must additionally capture dataset/split/rubric hashes, source and
scope snapshots, parser/artifact identity, model/provider/prompt/profile versions,
candidate/context/token/call/deadline budgets, repair/retry rules, repeat count, order
schedule, statistic settings, and the decision rule. Verify hashes before execution.
This directory does not yet contain that measured, reviewed freeze or run results.
