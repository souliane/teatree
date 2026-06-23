# BLUEPRINT Appendix — Rubric→verifier done-gate (#2241)

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §17.4. This is a member of the keystone-merge precondition family (§17.4.3) — a sibling of the [#1829](https://github.com/souliane/teatree/issues/1829) anti-vacuity attestation gate, built on the same SHA-binding and maker≠checker machinery.

## Why this gate exists

The standing rule is "**declare done only on a verified, full-spec outcome**." The recurring failure it guards against: a ticket reaches MERGED while its acceptance criteria are unverified — "declared done on a 2xx / a partial subset / an unrun test." That rule lived only in prose and personal memory; nothing mechanically refused the merge when the work was not actually done per spec.

This gate makes "done" objective and audited. Each ticket carries a **rubric** of N checkable acceptance criteria, and an **independent verifier sub-agent (≠ the maker)** grades the maker's output against every criterion. ALL must pass; the gate **fails closed** if any criterion is ungraded or unevaluable. It is the highest-value lever from the Fable-5 loop-design thread (a verifier sub-agent grading a checklist beats self-critique) and the structured form of acceptance criteria — it reinforces teatree's maker≠checker thesis, pushing it from the merge-safety gate down to "is the work actually done per spec."

## §17.4.3 precondition placement

The gate is a **keystone-merge precondition**, not a check on the ship FSM transition. The reason is the same one the anti-vacuity gate cites: the ship transition fires before a PR / live head SHA exists, so "every criterion PASS at the head SHA" is unevaluable there. The keystone merge is the only path to MERGED (raw `gh pr merge` / `glab mr merge` is hook-blocked), and it already holds the verified live head SHA plus the `MergeClear` (which carries the ticket FK).

Concretely, `core/merge/execution.py::assert_merge_preconditions` calls `_assert_rubric_satisfied(authorized_clear, live_sha)` immediately after `_assert_anti_vacuity(authorized_clear, live_sha)`, bound to the **same** just-verified `live_sha`. A force-push that moves the head off the reviewed tree therefore invalidates the CLEAR, the anti-vacuity attestation, and the rubric grades together — one staleness boundary, no replay window.

## Data model (`core/models/rubric.py`)

- **`Rubric`** — FK to `Ticket` (CASCADE, `related_name="rubrics"`). One active rubric per ticket: `Rubric.populate(ticket, criteria)` is a get-or-create that replaces the criteria atomically, so re-running `rubric-set` re-states the checklist rather than stacking duplicates, and resets every grade to PENDING (a stale PASS never carries over to a changed checklist). An empty criteria list is refused. `Rubric.objects.active_for_ticket(ticket)` returns the most-recently-created rubric.
- **`RubricCriterion`** — FK to `Rubric`, `ordinal`, `text`, `status` (`pending`/`pass`/`fail`, default `pending`), `grader_identity`, `reviewed_sha`, `rationale`, `graded_at`. A `UniqueConstraint(rubric, ordinal)`. The grade is recorded ONLY through the guarded `record_grade` factory.
- **`RubricError`** — raised by both `populate` and `record_grade` on a contract violation.

The record follows the durable, compaction-surviving pattern of `ReviewVerdict` / `MergeClear`: the DB row is the truth, and `record_grade` shares `MergeClear`'s validation primitives (`is_commit_sha`, `is_non_reviewer_role`) so the rubric-grade contract and the CLEAR/verdict contract cannot drift apart.

### The guarded grade factory

`RubricCriterion.record_grade(status, grader_identity, reviewed_sha, rationale)` refuses, before stamping:

- a non-terminal `status` (PENDING is not a grade — only `pass`/`fail`);
- an empty `grader_identity`, or one that `is_non_reviewer_role` classifies as a maker/coding-agent/loop role (the maker can never self-attest a criterion — the same guard `MergeClear.issue` / `ReviewVerdict.record` apply);
- a `reviewed_sha` that is not a full 40-char hex commit SHA (the grade binds to the exact reviewed tree, so the done-gate's head-equality check cannot silently fail on a truncated SHA).

### The fail-closed predicate

`Rubric.is_fully_passed_at(head_sha)` is `True` **iff** there is ≥1 criterion AND every criterion is PASS, graded by a non-maker identity, with `reviewed_sha == head_sha` (both lower-cased). Any other state is `False`: an empty rubric, any PENDING or FAIL criterion, a maker/empty grader, or a stale SHA. `Rubric.block_reason(head_sha)` names the first failing condition for the remediation message.

## The gate (`core/gates/rubric_gate.py`)

A pure function over the durable rubric row plus the live head SHA, mirroring `core/gates/anti_vacuity_gate.py`:

- `rubric_gate_required()` → `get_effective_settings().require_rubric_verification` (the new knob).
- `check_rubric_satisfied(ticket, head_sha, *, transition)` — NO-OP when the knob is off; otherwise passes only when the ticket's active rubric `is_fully_passed_at(head_sha)`, else raises `RubricNotSatisfiedError` with a remediation naming the `rubric-set` / `rubric-grade` CLIs.
- `_assert_rubric_satisfied(clear, head_sha)` (in `core/merge/authorization.py`) — NO-OP when the CLEAR has no ticket; else re-wraps `RubricNotSatisfiedError` as `MergePreconditionError` so the merge command's single re-escalation path surfaces it (the loop never self-issues a replacement CLEAR).

The gate **fails loud, never skip-as-pass**: a rubric that cannot be confirmed fully-passed blocks the merge. This is the standing "gate must fail loud" rule, and the anti-vacuous regression test pins it — with the gate call removed, the blocks-on-FAIL test goes RED.

## Configuration

`require_rubric_verification` (default `False` = NO-OP, purely additive — matches every other opt-in gate). Registered in `OVERLAY_OVERRIDABLE_SETTINGS`, so it is DB-home and per-overlay overridable — set it in the `ConfigSetting` store (a value left in `~/.teatree.toml` is ignored on read):

```bash
# global default stays off; dogfood the gate on one overlay only:
t3 <overlay> config_setting set require_rubric_verification true --overlay t3-teatree
```

## CLI seams

- `t3 <overlay> ticket rubric-set <ticket_id> --criteria-json '["AC1", "AC2"]'` (or `--criteria-file <path>`) — sets the criteria from EXPLICIT input. Auto-derivation from `/plan` is the [#2240](https://github.com/souliane/teatree/issues/2240) follow-up and is out of scope here. Accepts a JSON array of strings or of `{"text": ...}` objects; an empty / malformed / non-array payload is refused.
- `t3 <overlay> ticket rubric-grade <ticket_id> --grader-identity <verifier> --reviewed-sha <full-40-char-sha> --grades-json '[{"ordinal": 0, "status": "pass"}, ...]'` — records the verifier's per-criterion PASS/FAIL through the guarded factory. Criteria not named in the grades stay PENDING (fail-closed).

## Out of scope (follow-ups)

- Auto-deriving the rubric from `/plan` → [#2240](https://github.com/souliane/teatree/issues/2240) (this MR accepts explicit criteria only).
- Automating WHO dispatches the verifier sub-agent — this MR records a verdict; orchestration can reuse the existing review-dispatch machinery later.
- SDK-cutover of the eval grader (`eval/judge.py`'s `ClaudeJudge.grade`, currently `claude -p`). The LLM-grader prior art is kept SEPARATE from this DB-record path on purpose: extracting a shared grader would couple the metered-LLM path to the durable-record path and pull the SDK cutover into scope.
- Re-pending rubric grades on `reopen()` — out of scope for this first MR; the SHA-bind already invalidates stale grades when a new workstream moves the head.
