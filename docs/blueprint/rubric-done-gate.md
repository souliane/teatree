# BLUEPRINT Appendix — Rubric→verifier done-gate (#2241)

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §17.4. This is a member of the keystone-merge precondition family (§17.4.3) — a sibling of the [#1829](https://github.com/souliane/teatree/issues/1829) anti-vacuity attestation gate, built on the same SHA-binding and maker≠checker machinery.

## Why this gate exists

The standing rule is "**declare done only on a verified, full-spec outcome**." The recurring failure it guards against: a ticket reaches MERGED while its acceptance criteria are unverified — "declared done on a 2xx / a partial subset / an unrun test." That rule lived only in prose and personal memory; nothing mechanically refused the merge when the work was not actually done per spec.

This gate makes "done" objective and audited. Each ticket carries a **rubric** of N checkable acceptance criteria, and an **independent verifier sub-agent (≠ the maker)** grades the maker's output against every criterion. ALL must pass; the gate **fails closed** if any criterion is ungraded or unevaluable. It is the highest-value lever identified in an earlier loop-design retro (a verifier sub-agent grading a checklist beats self-critique) and the structured form of acceptance criteria — it reinforces teatree's maker≠checker thesis, pushing it from the merge-safety gate down to "is the work actually done per spec."

## §17.4.3 precondition placement

The gate is a **keystone-merge precondition**, not a check on the ship FSM transition. The reason is the same one the anti-vacuity gate cites: the ship transition fires before a PR / live head SHA exists, so "every criterion PASS at the head SHA" is unevaluable there. The keystone merge is the only path to MERGED (raw `gh pr merge` / `glab mr merge` is hook-blocked), and it already holds the verified live head SHA plus the `MergeClear` (which carries the ticket FK).

Concretely, `core/merge/execution.py::assert_merge_preconditions` calls `_assert_rubric_satisfied(authorized_clear, live_sha)` immediately after `_assert_anti_vacuity(authorized_clear, live_sha)`, bound to the **same** just-verified `live_sha`. A force-push that moves the head off the reviewed tree therefore invalidates the CLEAR, the anti-vacuity attestation, and the rubric grades together — one staleness boundary, no replay window.

## Data model (`core/models/rubric.py`)

- **`Rubric`** — FK to `Ticket` (CASCADE, `related_name="rubrics"`). One active rubric per ticket: `Rubric.populate(ticket, criteria)` is a get-or-create that replaces the criteria atomically, so re-running `rubric-set` re-states the checklist rather than stacking duplicates, and resets every grade to PENDING (a stale PASS never carries over to a changed checklist). An empty criteria list is refused. `Rubric.objects.active_for_ticket(ticket)` returns the most-recently-created rubric.
- **`RubricCriterion`** — FK to `Rubric`, `ordinal`, `text`, `status` (`pending`/`pass`/`fail`, default `pending`), `grader_identity`, `reviewed_sha`, `rationale`, `graded_at`. A `UniqueConstraint(rubric, ordinal)`. The grade is recorded ONLY through the guarded `record_grade` factory.
- **`RubricError`** — raised by both `populate` and `record_grade` on a contract violation.

The record follows the durable, compaction-surviving pattern of `ReviewVerdict` / `MergeClear`: the DB row is the truth, and `record_grade` shares `MergeClear`'s validation primitives (`is_commit_sha`, `is_independent_reviewer_identity`) so the rubric-grade contract and the CLEAR/verdict contract cannot drift apart.

### The guarded grade factory

`RubricCriterion.record_grade(status, grader_identity, reviewed_sha, rationale)` refuses, before stamping:

- a non-terminal `status` (PENDING is not a grade — only `pass`/`fail`);
- an empty `grader_identity`, or one that `is_independent_reviewer_identity` does not admit as an independent verifier (the maker can never self-attest a criterion — the same guard `MergeClear.issue` / `ReviewVerdict.record` apply);
- a `reviewed_sha` that is not a full 40-char hex commit SHA (the grade binds to the exact reviewed tree, so the done-gate's head-equality check cannot silently fail on a truncated SHA).

### The fail-closed predicate

`Rubric.unverified_reason(head_sha=None, *, waived=False)` is the ONE ladder both gates walk, and it names the first failing rung: no criteria -> PENDING -> FAIL -> uncited PASS -> non-independent grader -> stale-vs-head. The stale rung applies only when a `head_sha` is given, which is what separates the merge gate (bound to the live head) from the delivered gate (whose head has long since moved). Every rung NAMES the criteria that tripped it (`#<ordinal> '<text>'`), so a refusal points at what to fix rather than at a count. `waived=True` -- the human-authorized plan-bypass -- keeps exactly one rung, FAIL: a bypass says there was nothing to declare, never that the verifier's FAIL does not count.

`Rubric.is_fully_passed_at(head_sha)` is the one-line wrapper (`not unverified_reason(head_sha)`). Per-criterion truth lives once in `RubricCriterion.unverified_reason()`, which walks the same rungs, so `is_verified` / `is_passing_at` and the rubric-level refusal cannot disagree.

## The gate (`core/gates/rubric_gate.py`)

A pure function over the durable rubric row plus the live head SHA, mirroring `core/gates/anti_vacuity_gate.py`:

- `check_rubric_satisfied(ticket, head_sha, *, transition)` — the MERGE gate: passes only when the ticket's active rubric `is_fully_passed_at(head_sha)`, else raises `RubricNotSatisfiedError` with a remediation naming the `rubric-grade` CLI, and `plan-bypass` for a ticket with genuinely nothing to grade.
- `check_rubric_verified(ticket)` — the `mark_delivered` gate, registered as `rubric_verified`. Same ladder minus the head bind (a delivered ticket's head has moved on), so a missing rubric blocks exactly as the deleted spec-coverage manifest did.
- The audited waiver is the **human-authorized `ticket plan-bypass`** — the all-negatives manifest only `PlanArtifact.record_bypass` can write (`record` refuses that shape and names the escape). It waives a missing or ungraded rubric, including the phase-seeded process criteria the ticket never declared, but NEVER a recorded FAIL. A `none_reason` on an ordinary plan's `acceptance_criteria` is a reasoned negative like any other: it writes no rows and waives nothing.
- A PR **no ticket owns** is outside the gate's subject. `pr create` records every factory PR on the `PullRequest` ledger with its owning ticket, and a keystone CLEAR carries one, so an unresolvable ticket means a PR the factory did not author — no plan to grade and no ticket a bypass could be recorded on. `core/merge/ticket_gates.py` therefore SKIPS the rubric gate there; the setting-scoped anti-vacuity refusal still fires, because that one has an operator remedy.
- `_assert_rubric_satisfied(clear, head_sha)` (in `core/merge/authorization.py`) — NO-OP when the CLEAR has no ticket; else re-wraps `RubricNotSatisfiedError` as `MergePreconditionError` so the merge command's single re-escalation path surfaces it (the loop never self-issues a replacement CLEAR).

The gate **fails loud, never skip-as-pass**: a rubric that cannot be confirmed fully-passed blocks the merge. This is the standing "gate must fail loud" rule, and the anti-vacuous regression test pins it — with the gate call removed, the blocks-on-FAIL test goes RED.

## Configuration

None. The gate is unconditional on both transitions — there is no setting that disables it, because a flag is what let the behaviour ship inert. The escapes are the audited, recorded ones:

- `ticket plan-bypass <id> --human-authorize <who> --reason <why>` — **the only rubric waiver**, and it never overrides a recorded FAIL.
- `ticket skip-planning <id> --reason <why>` — escapes the PLAN gate only. The rubric gate still grades whatever criteria the ticket carries.

## The producer

The gate is unconditional, so a rubric nobody grades refuses the merge forever. The **cold reviewer is the producer**, and it is the only automatic one: it already IS the independent verifier the gate requires (grader != maker, one per dispatched head), it is the one actor that read the tree, and it runs on every PR the factory opens — so nothing extra has to be dispatched.

It returns its grades in the SAME envelope as its verdict. `agents/result_schema.ReviewVerdictEnvelope` carries `rubric_grades: list[RubricGrade]`, and `agents/review_envelope_recorder.record_returned_review_envelope` — the orchestrator, a different actor than the reviewer — stamps them through `Rubric.apply_grades`. `rubric_grades` is deliberately NOT in the schema's `required` list: whether the reviewed ticket HAS a rubric is a DB question only the recorder can answer.

Three properties carry the design:

- **The graded ticket is resolved from the PR identity.** `core/merge/ticket_resolution.gated_ticket_for_review_task` calls the IDENTICAL `resolve_gated_ticket` this gate reads at merge time. A reviewing task's own `task.ticket` is a REVIEWER-ROLE row keyed by the PR url (`core/models/auto_review_dispatch.py`, `loop/persistence_self_pr_review.py`), so the rubric the merge will grade is unreachable from it. Writer's key == reader's key, or the grade lands where no gate looks.
- **Coverage is checked BEFORE any write.** `ReviewVerdict.record` retires the per-head dispatch claim and releases the `MRReviewLock` in the same transaction that records the verdict. A verdict written over a half-graded rubric would therefore leave nothing to re-arm review while this gate went on refusing the merge — the head unmergeable forever, with no reviewer left to fix it. So a returned grade set that leaves any criterion PENDING records NOTHING.
- **The verdict and the grades share ONE atomic.** A grade the guarded factory refuses (an uncited PASS, a maker grader) rolls the verdict, the claim retirement and the lock release back with it, so the head stays re-reviewable.

The grader identity is the verdict's own (`reviewer_identity`, defaulting to `headless-reviewer`) and the SHA is the DISPATCH head, never the reviewer's self-asserted one — a grade can never vouch for a tree the verdict does not.

A refusal is stamped with `agents/envelope_refusal.MALFORMED_RUBRIC_GRADES_PREFIX`, a member of `_RECORDER_REFUSAL_MARKERS` beside `MALFORMED_FIX_RECORD_PREFIX`, so the taxonomy classes it an ENVELOPE refusal earning `transient_requeue`'s one-shot corrective retry rather than paging a human over a defect.

The reviewer is TOLD the checklist, not just told to grade one: `agents/dispatch_preflight.rubric_brief_lines` renders a `TICKET RUBRIC (ticket <pk>)` block listing each `#<ordinal> <text>` into the reviewing brief, resolved through the same `gated_ticket_for_review_task`. No block means no rubric and no grades owed.

A PR **no ticket owns** and a **rubric-less ticket** owe no grades at all — byte-for-byte the subject `core/merge/ticket_gates.py` already skips.

## CLI seams

- The PRIMARY producer is the plan: `PlanArtifact.record` turns the manifest's `acceptance_criteria` into rubric rows (`Rubric.add_criteria`, additive and idempotent on text, so a `plan-reaffirm` never resets a grade).
- `t3 <overlay> ticket rubric-set <ticket_id> --criteria-json '["AC1", "AC2"]'` (or `--criteria-file <path>`) — RESTATES the criteria from explicit input, resetting every grade. Accepts a JSON array of strings or of `{"text": ...}` objects; an empty / malformed / non-array payload is refused.
- `t3 <overlay> ticket rubric-grade <ticket_id> --grader-identity <verifier> --reviewed-sha <full-40-char-sha> --grades-json '[{"ordinal": 0, "status": "pass", "rationale": "<what proves it>"}, ...]'` — the OPERATOR's manual seam beside the reviewer producer above, for a rubric no review will grade. It is not the reviewer's path: a grade written here lands outside the transaction that records the verdict, so a refused grade could not roll the verdict back. It records the verifier's per-criterion PASS/FAIL through the guarded factory. A PASS without a rationale is refused; the rationale is free-form and may cite any test kind. Criteria not named in the grades stay PENDING (fail-closed).

## Out of scope (follow-ups)

- Sharing a grader with the eval LLM-judge (`eval/judge.py`'s `ClaudeJudge.grade`, an in-process `claude-agent-sdk` call). The two are kept SEPARATE on purpose: extracting a shared grader would couple the metered-LLM path to this durable-record path.
- Re-pending rubric grades on `reopen()` — out of scope for this first MR; the SHA-bind already invalidates stale grades when a new workstream moves the head.
