---
name: ship
description: Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests. Use when user says "commit", "push", "PR", "merge request", "pull request", "finalize", "deliver", "ship", or is in the delivery phase.
compatibility: macOS/Linux, git, glab or gh CLI, CI system.
requires:
  - workspace
  - rules
companions:
  - finishing-a-development-branch
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Delivery

## Delegation

This skill delegates the generic branch-finalization doctrine to:

- `finishing-a-development-branch` — decide how to wrap up a ready branch
- `verification-before-completion` — fresh verification before claiming the branch is ready

Optional [obra/superpowers](https://github.com/obra/superpowers) companions provide generic methodology. TeaTree keeps the project-specific workflow locally.

From "code is done" to "PR is merged."

### Dispatching a t3:shipper sub-agent (Non-Negotiable)

When the orchestrator dispatches a `t3:shipper` sub-agent, the brief MUST carry the **exact MR/PR title** and a **description skeleton** — never leave the subject for the sub-agent to improvise. The title format is gated (the active overlay's `validate_pr` rejects a non-conforming first line at push), so an improvised subject is the real cause of off-target titles and failed pre-push validation: the sub-agent guesses a subject, it fails the gate, and the run burns a retry. Compose the title in the orchestrator from the ticket data and hand it over verbatim.

- **Title** — supply the full first line in the active overlay's format: `type(scope): description [flag] (TICKET_URL)`. Use only the overlay's allowed `type` values and include the trailing reference only where the overlay's `require_ticket` demands it (§ 0); the orchestrator reads `TICKET_URL` from `.t3-env.cache`, never the sub-agent.
- **Description skeleton** — supply the headers the overlay expects (e.g. What / Why, plus `Open questions & assumptions`) pre-filled with the ticket's intent, so the sub-agent fills prose, not structure.
- The sub-agent may refine wording but MUST NOT change the `type(scope)`, the trailing reference, or invent a new subject.

## Dependencies

- **workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.

## Workflow

### 0. Ticket-Required Overlay Gate

When the active overlay has `require_ticket = True`, refuse to commit or push without a ticket reference.

- **Detection:** check `overlay.config.require_ticket`. Overlays that dogfood their own workflow enable this flag.
- **Every commit must include** an issue reference. Default to a ticket-URL parenthetical at the end of the subject line (`type(scope): description (TICKET_URL)`) — that is the linking mechanism the active overlay's `OverlayMetadata.validate_pr(title, description)` enforces (surfaced as the `t3 tool validate-mr` CLI command and the PreToolUse hook in § "PR / MR Creation"). When the active overlay sets `mr_close_ticket = True` (the teatree overlay does), the ship path keeps `Closes/Fixes #<number>` so a merged PR auto-closes its issue **by default** — `should_close_ticket` rewrites it to `Relates to #N` only when `Ticket.extra['more_prs_coming']` is set (a declared partial, or an umbrella with remaining tracked scope). Overlays that leave `mr_close_ticket = False` (the class default) never auto-close.
- **If no ticket context exists:** ask "Which ticket is this for?" Do not proceed without a ticket reference. The full procedure for resolving a missing reference — and when you may create one vs. when you must ask — is the **Missing Issue Reference Policy** in § 0a below.
- **Exception:** commits from `/t3:retro` (format `fix(<skill>): ...`) are exempt — retro findings are small tactical fixes committed directly on the current branch.

### 0a. Missing Issue Reference Policy (Non-Negotiable)

When a commit or MR/PR needs an issue/ticket reference and you have none in hand, **never improvise** — do not invent a dummy/placeholder reference, and do not auto-file an issue on a tracker you do not own. Follow this two-step policy. It is encoded in the DB-home `missing_issue_ref_policy` setting (`t3 <overlay> config_setting set missing_issue_ref_policy <value>`, `--overlay <name>` for the per-overlay scope; `T3_MISSING_ISSUE_POLICY` env var) and resolved deterministically by `teatree.missing_issue_policy.resolve_missing_issue_verdict(colleague_facing=…, existing_found=…)`; this prose is the agent-facing contract the setting enforces.

1. **Find the ORIGINAL existing issue first (always, every policy tier).** Before anything else, look for the issue that already covers this work — the one that introduced the bug, or the one that left the scope unimplemented. Search the repo's issues (open **and** closed) and the introducing commit's linked issue:

   ```bash
   git log -S '<the buggy line/symbol>' --oneline       # find the introducing commit
   git show <introducing-sha> --format='%s%n%b' | grep -iE '#[0-9]+'  # its linked issue
   gh issue list --search '<keywords>' --state all       # GitHub
   glab issue list --search '<keywords>'                 # GitLab
   ```

   If you find it, **use that reference** — it is the canonical one. Do not open a new issue that duplicates an existing one.

2. **If no existing issue is found, the fallback depends on the repo class** (the same colleague-facing-vs-own distinction the [`../rules/SKILL.md`](../rules/SKILL.md) § "Three Orthogonal Repo Axes" tracks):

   - **Colleague-facing / external repos** (a shared product repo of an org — one the user does **not** own): under the default policy you must **ASK the user** for the reference (via `AskUserQuestion`). NEVER auto-create an issue and NEVER use a dummy/placeholder reference on a tracker the user does not control.
   - **The user's own repos** (teatree itself, the user's solo overlay repos): creating an issue is allowed without asking — the user owns the tracker, so a created issue is self-bookkeeping, not noise on a colleague's surface.

**Default (`find_existing_then_ask`):** find-existing-first, then ask on colleague repos / create on own repos, never a dummy. `create` and `dummy` are **opt-in only** (`config_setting set missing_issue_ref_policy create` / `dummy`, or with `--overlay <name>`) — they authorise auto-create / a placeholder reference even on a colleague-facing repo, and are OFF by default. When the resolver returns `ASK_USER`, surface the blocker and wait — do not fill the gap yourself.

### 1. Commit

- **Run `prek install` before the first commit in any worktree (Non-Negotiable).** This wires prek as the git pre-commit hook runner. Without it, whatever hook runner the repo happens to have (or nothing) runs on `git commit` — you cannot rely on prek's quality gates. This applies in every worktree, including colleagues' worktrees and review worktrees.
- **Never commit to the default branch (Non-Negotiable).** Run `git branch --show-current` before every commit. If you are on `main`, `master`, `development`, or `release`, STOP — create a feature branch first (`git checkout -b <prefix>-<repo>-<topic>`), then commit there. This applies even for "quick fixes" and hotfixes with no ticket.
- **Verify branch matches ticket** before committing. If on the wrong branch, create a clean branch from the default branch and cherry-pick.
- **Before committing to a branch you did not create this session, check its PR isn't already merged/closed.** A squash-merge leaves the local and remote branch present, so the "refuse commits on a merged-PR branch" pre-commit guard does not fire (it only catches a *deleted* remote). Committing onto a long-lived branch from a prior session then pushes an orphan commit that rides no open PR and never reaches the default branch. `gh pr list --head <branch> --state all` (or `glab mr list --source-branch <branch>`) before staging; if the PR is merged, branch off the freshly-fetched default branch for the follow-up work and open a new PR.
- **Check for pre-existing changes before staging.** If the diff includes changes you did not make in this session, **warn the user** — either stage only your hunks or ask how to proceed.
- Format commit message following the project's commit format reference.

**Quick commit recipe (the canonical HOW).** Stage and commit with a bare `git commit -m` so the hooks run normally — never `--no-verify`, and never a `Co-Authored-By` trailer (the user's global config forbids it; see § Rules):

```bash
# Single staged change, clean message, no trailer:
git commit -m "type(scope): description"

# Touching files the linter may reformat? stage everything in one shot:
git commit -a -m "type(scope): description"
```

Do X — never Y: DO compose the message with a bare `git commit -m`. NEVER pass `--no-verify`. NEVER append a `Co-Authored-By:` line. Body content (Open questions & assumptions, the `Closes/Fixes #N` keyword) goes in additional `-m` blocks or via a `git commit` editor session, never as a trailer.

- **Carry an `Open questions & assumptions` section in the commit message body** (one bullet per item, status `decided-by-user` / `assumed` / `open`; `- none` when there is nothing to flag). Same content also goes in the PR description — see § 5 "Open Questions & Assumptions" for the canonical rule.
- **Link commits to issues** via the ticket-URL parenthetical in the subject line (`type(scope): description (TICKET_URL)`) **when the active overlay has `require_ticket = True`** (see § 0). Overlays with the default `require_ticket = False` (teatree itself) do NOT need the URL — a plain `type(scope): description` subject is correct and the overlay's `validate_pr` (the base-class no-op for teatree) will not reject it. With `mr_close_ticket = True` the ship path keeps a `Closes/Fixes #<number>` body keyword by default (the issue auto-closes on merge); set `Ticket.extra['more_prs_coming']` to suppress that for a declared partial or an umbrella with remaining scope (`should_close_ticket` then emits `Relates to #N`).
- Read `TICKET_URL` from `.t3-env.cache` (the per-worktree symlink to `.t3-cache/.t3-env.cache`) — never construct it from the branch name.
- **No commit-type bypass for the quality gates.** The teatree `quality-gates` and `module-health` hooks have no `relax:` escape hatch (souliane/teatree#525). When the gate fires, fix the architecture: split the file by concern, refactor module-level functions onto a class, replace `dict[str, object]` with a typed dataclass, or delegate the suppressed import/call to a module the hook already exempts (`tests/`, `scripts/hooks/`, `e2e/`, `skills/`, `docs/`). The legitimate house pattern around `subprocess` (`# noqa: S404` on the import, `# noqa: S603` on each call) belongs in a CLI helper module that already lives under one of those exempt paths — not in newly suppressed source files.

### 2. Finalize Branch

- `t3 <overlay> workspace finalize [msg]` — squash commits + rebase on default branch.
- Run in each repo that has changes.
- Verify the commit message follows the project's format.

**Squash rules:**

- **Use `git reset --soft`, not interactive rebase.** `git rebase -i` with custom editors is fragile when pre-commit hooks run on each commit. Use `git reset --soft $(git merge-base origin/<default-branch> HEAD) && git commit` to squash, or cherry-pick for non-adjacent commits.

  **Quick squash recipe (the canonical HOW).** To collapse several `wip` commits into one clean commit before the PR merge — do this, never `git rebase -i` and never a raw `gh`/`glab` merge of the noisy history:

  ```bash
  # Squash all local-only commits on this branch into one staged set, then re-commit:
  git reset --soft $(git merge-base origin/main HEAD) && git commit -m "type(scope): description"
  ```

  Swap `origin/main` for the repo's actual default branch (`origin/master`, `origin/development`). `t3 <overlay> workspace finalize "type(scope): description"` does exactly this and resolves the merge-base correctly — prefer it when available.
- **Never rewrite pushed history** — see § Rules below for the full statement. Before any squash, check `git log origin/<branch>..HEAD` to confirm which commits are local-only.
- Group by topic, keep human-sized commits.
- Squash integrity check: save `OLD_TIP=$(git rev-parse HEAD)`, verify `git diff $OLD_TIP..HEAD` is empty after rewrite.
- Respect `T3_AUTO_SQUASH` (`true` = auto, `false` = ask first).
- **Always use `git merge-base`** for the squash target. NEVER use `origin/master` or `origin/main` directly — the branch may have been created from a stale local copy, causing the squash to include unrelated commits. The `t3 <overlay> workspace finalize` command handles this correctly.

### 3. Local Verification

- Start servers and verify functionality.
- **E2E gate:** If the project requires E2E tests for the type of changes made (UI, forms, user flows), those tests must be written and passing BEFORE proceeding. E2E is part of implementation, not a post-push activity.
- **Wait for user feedback.** Do NOT proceed to push without user approval.

### 3a. BLUEPRINT.md Sync

If the changes touch architecture, add new modules, rename commands, or change extension points:

1. Read `BLUEPRINT.md` and check if it reflects the current state.
2. If it doesn't, update it **before** pushing. Ask the user before modifying.
3. This applies to all repos that have a `BLUEPRINT.md`.

### 3a1. Documentation Discipline (Non-Negotiable)

Before creating the PR, ask: did this diff change anything a user or colleague would learn from the README, BLUEPRINT, or any skill file?

Common triggers (not exhaustive):

- New `t3` command, flag, or env var
- Renamed or removed public symbol, command, or setting
- New FSM state, lifecycle phase, or BLUEPRINT-keyed concept (e.g. a new `Ticket.State`, a new `LoopLease` row name, a new `MiniLoopMarker` name)
- New `SKILL.md` added, or one removed
- User-observable behaviour change (default flips, UI flow, error message shape, response payload)
- New feature flag

**If YES:** the same MR includes the doc update — pick the file by the trigger, then start editing it before `pr create`:

| Trigger | Doc to update |
|---|---|
| New `t3` command / flag / env var | `README.md` (user-facing usage) |
| New `Ticket.State` / FSM phase / `LoopLease` / `MiniLoopMarker` name | `BLUEPRINT.md` |
| New `SKILL.md` added (or one removed) | the top-level `README.md` skills catalogue |
| Skill behaviour change | the relevant `SKILL.md` |

```bash
# YES path — open the matching doc to add the entry (canonical HOW; e.g. a new SKILL.md):
$EDITOR README.md          # skills catalogue, or the user-facing command doc
$EDITOR BLUEPRINT.md       # for a new FSM state / lifecycle concept
```

**If NO:** the MR description carries this attestation line on its own — record it directly, do NOT touch README/BLUEPRINT:

```text
docs: n/a — <one-line reason>
```

```bash
# NO path — append the attestation to the PR body draft (canonical HOW):
echo "docs: n/a — <one-line reason>" >> .git/PR_BODY.md
```

Examples:

- `docs: n/a — internal refactor, no user-visible change`
- `docs: n/a — bug fix preserving existing contract`
- `docs: n/a — test-only change`
- `docs: n/a — generated-doc regeneration, source unchanged`

The line is the friction-free attestation. Reviewers read it; if the reason looks wrong they push back on the specific reason, not on a generic "did you update docs?" prompt.

**How the deterministic gate divides the work.** The unambiguous triggers (new top-level `t3` command, new `SKILL.md`, new `Ticket.State` value, new `LoopLease` / `MiniLoopMarker` name) are caught by `scripts/hooks/check_doc_update.py` automatically — the pre-push prek hook and the `doc-update-gate` CI job fail the push when the matching README/BLUEPRINT diff is missing. The skill prose above handles the soft cases the hook cannot safely judge.

Both layers (the gate and the attestation) run on every PR — the gate runs deterministically, the attestation is the reader's signal that the agent considered docs and made a deliberate call.

### 3b. Self-Review Against Repo Rules

**Before every push**, run the self-review gate from [`../review/SKILL.md`](../review/SKILL.md) § "Active Verification Against Repo Rules":

1. **Load the project's code-review skill** (e.g., `/code-review`) if available. This skill contains the exact rules enforced by automated review bots — loading it prevents multi-round push-fix-push cycles.
2. **Read** the repo's `AGENTS.md` (or equivalent agent instructions file).
3. **For each changed file**, verify compliance against every applicable rule — commit message format, architectural patterns, banned patterns, feature flags.
4. Fix any violations **before** pushing.
5. **Run the full CI-equivalent local gate set:** `t3 tool verify-gates`. It runs BOTH `prek run --all-files` AND `prek run --all-files --hook-stage pre-push`, so the push-stage gates (comment-density, doc-update, ensure-pr, the public-repo leak gate) — which a bare `prek run --all-files` STRUCTURALLY skips but CI re-runs — are exercised locally. Report its exit code as the green-proof; a commit-stage-only run is not proof.

Skipping this step is the #1 cause of wasted push-fix-push cycles. The rules exist in `t3:review` and the project's code-review skill — this step ensures they are applied even when the agent goes directly from code to ship without a formal review phase.

### 3c. Emit Retro Signal Before Push — Do NOT Self-Retro (#837)

Retro is **orchestrator-only**. A sub-agent shipping a single ticket does **not** run `/t3:retro` as a per-ticket synthesis/judgment step, and `pr create` does **not** gate on a per-ticket `retro` visit. Instead, as the work surfaces a lesson, **emit it as structured signal into durable state** (task metadata / a `/tmp/t3-snapshot-*.md` snapshot) so the orchestrator can synthesise across the whole session later and bias the output to the smallest enforcement artifact (a gate, test, or hook), not a prose rule.

Sequence: code → test → review gate → **local commit** → push → PR → monitor CI. (The orchestrator's periodic synthesis runs out-of-band over the accumulated durable signal — it is not in this per-ticket push path.)

If you do produce a same-PR-worthy fix while shipping (a stale doc, a broken reference the change touches), commit it on the current branch before § 4 Push — that is ordinary in-scope bundling, not a retro step.

### 4. Push

- **Reconcile with the default branch first.** `git fetch origin <default> && git log <branch>..origin/<default> --oneline` — if any commits appear, merge them in (`git merge origin/<default>`) and re-run lint/tests before pushing. Opening a PR that is already BEHIND main forces the user (or you) to do a second round-trip to resolve conflicts; catch them now, while you have the context of your own change open.
- Push to remote. Cancel stale pipelines first if the branch has an existing PR (see § Rules).

### 4b. Review Gate (Non-Negotiable)

Before creating a PR, the `pr create` command automatically checks the session gate:

- **shipping** requires prior `testing` and `reviewing` phases. (#837: it no longer requires a per-ticket `retro` visit — retro is orchestrator-only.)
- If no review session ran for this ticket, `pr create` returns an error with a hint to run `/t3:review`.
- `--skip-validation` is reserved for bypasses the **user explicitly authorised** in the same session — never the agent's own choice. It skips only the **heavy** gates (visual QA, branch currency, FSM phase check) and STILL runs the cheap MR title/description format check (#1486), so a non-canonical title can never slip onto the remote via this bypass. The separate `--skip-mr-format-check` is the explicit opt-in that disables that format check too — needed only in the rare case a non-canonical title must ship anyway (and likewise user-authorised, never the agent's own choice).

**The `reviewing` phase MUST be earned by spawning the `t3:reviewer` sub-agent — not by self-review against repo rules alone.** § 3b ("Self-Review Against Repo Rules") is necessary but not sufficient: it is the implementer reviewing their own work, which is the exact pattern that allowed #545 to claim "implementation finished" while review later found six rounds of missed renames, broken tests, undocumented contract changes, and a bypassed shipping gate. An independent reviewer that has not seen the implementation conversation is the corrective.

#### Single source of truth: the session feeds the FSM (#694)

Teatree has **two stores**, but they can no longer disagree:

1. **`Session.visited_phases`** — the **single source of truth**. Both the loop path (`Task.complete()` → `_record_phase_visit()`) and the CLI path (`lifecycle visit-phase`) write canonical phase tokens here. `lifecycle visit-phase` resolves the ticket by pk / issue number / issue URL (same identifier set as `pr create`), normalizes the phase name (short verbs and gerunds both work), and logs a WARNING + reports the resulting state when a transition is not legal — out-of-order calls fail loudly, never silently.
2. **`Ticket.state` FSM** (`STARTED → CODED → TESTED → REVIEWED → SHIPPED → IN_REVIEW → ...`) — `django_fsm` transitions on `core.models.ticket.Ticket`. At the shipping gate, `_check_shipping_gate` **reconciles `ticket.state` from `visited_phases`**: when the required phases are present it auto-walks the FSM to `REVIEWED` (`reconcile_reviewed()`) so `ticket.ship()` is legal; when phases are missing it blocks with the exact missing-phase list. `pr create` therefore **never raises a raw `TransitionNotAllowed`** — it either ships or returns a structured gate failure. This invariant holds on every path: missing phases, **no session at all** (no attested work ⇒ structured failure, not a silent pass), and **`--skip-validation`** (the un-reconciled `ship()` is wrapped, so an illegal hop becomes the same structured failure).

**Still prefer driving transitions through task completion.** It keeps the task ledger clean: `Task.complete()` → `_record_phase_visit()` records the phase, `_advance_ticket()` fires the matching FSM transition → `_consume_pending_phase_tasks` clears stragglers → next-phase task auto-scheduled. `lifecycle visit-phase` is a valid fallback on the human/CLI path — the gate reconciles either way — but the reviewing phase must still be **earned by spawning the `t3:reviewer` sub-agent** (see § 4b above), not self-attested to skip review.

The gate verifies the required phases (`testing`, `reviewing`, `retro`) were recorded for the work — nothing more. Independence in code review is a property of the **execution context**, not of a stored identity: the `reviewing` phase is earned by spawning a fresh `t3:reviewer` sub-agent that has not seen the implementation conversation, and that spawn boundary *is* the independence guarantee, by construction. A same-session spawn is fine and preferred. There is no `agent_id` comparison — the same identity recording `coding` and `reviewing` does not block the gate, because string-identity inference added no real independence over the structural spawn boundary. The shipping gate evaluates the **union of `visited_phases` across all of the ticket's sessions** (`Ticket.aggregate_phase_records()` → `Session.check_gate_across_ticket`), not the latest session alone — the single source of truth is the ticket's lifecycle, not one session. `phase_visits` is retained purely as an audit trail of who recorded each phase; it is not consumed for gate enforcement.

#### How to satisfy the gate (the only sanctioned path)

1. **Locate the reviewing task.** When the worktree was created via `t3 <overlay> workspace ticket <url>`, a `Task(phase="reviewing")` was scheduled by `Ticket.schedule_review()` once `test()` fired. If the branch is ad-hoc (no session/ticket exists yet), create one first with `t3 <overlay> workspace ticket <issue_url>` — never push from a session-less branch. The session is the receipt; without it the FSM has nothing to advance.

2. **Spawn the reviewer sub-agent from the main conversation** (not from another sub-agent — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Sub-Agent Limitations") via the `Agent` tool:

   The `prompt:` MUST open with this verbatim block — it is not optional and not a "remember to add it" note. Skill prose does not propagate into a spawned agent's context, so the near-zero-comments rule is lost unless it is inline in the prompt itself:

   ```text
   NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
   ```

   ```text
   Agent(
     description: "Pre-push review of <ticket>",
     subagent_type: "t3:reviewer",
     prompt: "<the verbatim block above>, then: \
              Review the diverging code on this branch against main. Branch: <name>. \
              Ticket: <url>. Scope: <one-line summary>. Read the linked ticket end-to-end \
              before touching the diff (per /t3:review § 'Step 0 — Gather Ticket Context'). \
              Apply both the per-file repo-rules check (/t3:review § 'Active Verification \
              Against Repo Rules') and the module-level architectural check (full files of \
              every touched module). Report findings as a punch list — no code edits."
   )
   ```

3. **Apply every finding.** Reviewer sub-agents are read-only; the implementing conversation owns the edits. Do not cherry-pick which findings to take — if a finding is wrong, push back with evidence in the same conversation, do not silently drop it.

4. **Drive the `review` transition** so the FSM advances `TESTED → REVIEWED` and the shipping task is auto-scheduled. Two equivalent entry points (use the first one available):

   - **Preferred — complete the reviewing task**, which auto-fires `ticket.review()` via `Task._advance_ticket()`. This matches how every other phase advances and keeps the task ledger clean.
   - **Direct transition fallback** when the agent doesn't have the task ID handy: `t3 <overlay> ticket transition <ticket_id> review`. The FSM still requires a completed `reviewing` task as a `conditions=` predicate, so this only works after step 3 has already produced one.

   Do **not** use `t3 <overlay> lifecycle visit-phase <ticket_id> reviewing` to *skip* an independent review. Since #694 the gate reconciles `Ticket.state` from `Session.visited_phases`, so a manual visit *will* let `pr create` proceed — which is exactly why marking `reviewing` visited without an independent reviewer having actually reviewed the diff defeats the quality gate. Earn the phase (step 2), then record it; never record it to dodge step 2.

5. **Verify before pushing.** `t3 <overlay> ticket list --state reviewed` (or `--id <ticket_id>`) should show the ticket in `reviewed` once the reviewing task completed. If the loop path advanced phases but the state still reads `tested`/`started`, that is expected — the shipping gate reconciles it to `reviewed` at `pr create` time. A blocked `pr create` returns the missing-phase list (`testing` / `reviewing` — #837: never `retro`); satisfy those phases (run the reviewer), don't bypass with `--skip-validation`.

**Why a sub-agent, not just self-review.** The implementer's context is contaminated by the implementation: every "looks done" judgment carries the same blind spots that produced the gaps. A sub-agent starts cold, reads the diff with fresh eyes, and applies the review skill's checklists without the implementer's "I already checked that" shortcuts. The cost of one extra `Agent` call is ~30s of wall time and a few hundred tokens; the cost of skipping it is multi-round push-fix-push cycles after the PR is already public.

**Why the FSM, not just the session gate.** The session gate is a soft safety net that fail-opens when no session exists. The FSM is the actual contract — every downstream consumer (`pr create`, `execute_ship`, the loop dispatcher, the statusline) reads `Ticket.state`. Bypassing the FSM produces tickets that look reviewed in the session record but are still `TESTED` in the source of truth, which then breaks every later transition until someone notices and rewinds by hand.

### 4c. Visual QA Gate

`pr create` also runs a pre-push browser sanity gate as a side effect of the shipping flow. It loads the page(s) the branch diff actually touches and reports silent-render regressions (page crashes, console errors, raw `app.*` translation keys, blocking asset 404s). Target URLs come from the overlay's `get_visual_qa_targets(changed_files)` — overlays opt in by mapping diff paths to URLs.

- Runs automatically before PR creation; the report is recorded on `Ticket.extra['visual_qa']`.
- Blocks PR creation when findings exist; the error payload includes `report_markdown` for a `## Visual QA` section.
- Bypass: `t3 <overlay> pr create <ticket> --skip-visual-qa "<reason>"` or `T3_VISUAL_QA=disabled` in the environment.
- Skipped when Playwright cannot start — fails open with a clear message rather than blocking the push.

### 5. Create MR/PR

**`t3 <overlay> pr create` is mandatory (Non-Negotiable).** Raw `gh pr create` / `glab mr create` is forbidden whenever the active overlay exposes a `pr create` subcommand. The CLI is not a convenience wrapper — it is the **only** path that runs the shipping gate (§ 4b: testing + reviewing phases visited), the visual-QA gate (§ 4c), the title/description format validator, the ticket URL injection, the assignee defaults, and the fork-vs-upstream remote resolution. Bypassing it ships PRs that look published but have skipped every guard — exactly the failure mode that produced souliane/teatree#545 (PR pushed via `gh pr create`, shipping gate never ran, six rounds of follow-up review fixes).

**Allowed exceptions (must hold all):**

1. The active overlay genuinely has no `pr create` subcommand (e.g., a brand-new overlay still being built). Verify with `t3 <overlay> pr --help` before assuming.
2. You are fixing the `t3 <overlay> pr create` command itself and need a one-shot bypass to land the fix.
3. You explicitly state the bypass and the reason in the response, so the user can stop you if the assumption is wrong.

If `t3 <overlay> pr create` errors, **fix the error** (create the missing session, run the reviewer, set the missing env var) — do not work around it with raw `gh`/`glab`. Treating the CLI as optional is the same anti-pattern as "Fix the CLI, Never Work Around It" in [`../workspace/SKILL.md`](../workspace/SKILL.md), applied to the shipping flow.

**If the PR/MR needs an issue reference and you have none, do NOT improvise a dummy ref or auto-file on a tracker you do not own** — follow § 0a "Missing Issue Reference Policy": recover the original existing issue first, then ask on a colleague-facing repo (or create on the user's own repo) per the resolved `missing_issue_ref_policy`.

#### Scope-Match Gate Before `Closes/Fixes #N` (Non-Negotiable)

The commit body keeps `Closes/Fixes #N` and the issue auto-closes on merge **by default** (overlay `mr_close_ticket = True`). Before relying on that, re-read the linked issue end-to-end and **enumerate every acceptance criterion, phase, or deliverable it names**. For each one, mark it ✅ shipped, ⚠️ partial, or ❌ not started, and paste the matrix into the PR body. Auto-close is correct only when every row is ✅. Otherwise (a partial, or an umbrella with remaining tracked scope):

- Set `Ticket.extra['more_prs_coming'] = True` before `pr create` so `should_close_ticket` rewrites the body keyword to `Relates-to #N` and the issue stays open.
- List the unshipped phases/AC in the PR body under a "Remaining scope" heading so the next agent sees the gap.
- Do NOT rely on "I'll do the rest later" memory. The issue body is the contract; a partial PR that auto-closes the issue silently discards the rest of the contract.

#### Per-Namespace Close-Trailer Gate (`ban_close_trailers_on_namespaces`)

Some namespaces drive their issue lifecycle through a separate workflow and forbid the platform's auto-close behaviour entirely. `ban_close_trailers_on_namespaces` is a DB-home setting — configure those namespaces in the `ConfigSetting` store:

```bash
t3 <overlay> config_setting set ban_close_trailers_on_namespaces '["my-group/*"]'
```

When the target PR/MR repo matches one of these fnmatch patterns and the body still carries a `Closes|Fixes|Resolves` trailer (the `part of` and full-URL variants too), `ShipExecutor._build_pr_spec` silently strips those lines before opening the PR — the publish proceeds, the issue does not auto-close on merge. Default empty list keeps legacy behaviour. This is the user-scoped sibling of the overlay-scoped `forbid_close_keywords` gate (#1012) which refuses the publish entirely.

**STOP — resolve the ticket URL before typing the glab command.**

Before composing any `glab mr create` or `glab mr update` call, answer these three questions:

1. **What is the ticket URL?** Find the GitLab issue/work item URL from context. If none exists, create one now (`glab issue create`) and copy the URL. Do NOT proceed without a URL.
2. **What is the feature flag?** Use `[none]` if there is no flag.
3. **Is the title in the exact format?** `type(scope): description [flag] (ticket_url)` — both `--title` and the first line of `--description` must be identical and match this format exactly.

This gate exists because the overlay's own CI MR-title validator (the validating overlay mirrors the same `validate_pr` rules in its `release_notes/validate_mr.py`, packaged as `src/<overlay>/mr_validation.py`) fails on every PR that skips the ticket URL, causing a red pipeline that requires a separate fix push. The CI validator is the safety net — not the first line of defence. **Always resolve the URL before creating the PR.**

**Read the project's delivery hooks reference** (e.g. `references/delivery-hooks.md`) for the concrete PR creation template. Critical rules:

- **PR title = squash commit message** (PRs use squash-before-merge, so the title becomes the final commit). It MUST include the ticket URL: `type(scope): description [flag_if_feat] (TICKET_URL)`
- **PR description first line = same format as title** (CI validates it). NEVER start with `## Summary` — that fails validation.
- **Always assign to the user.** The `t3 <overlay> pr create` command handles the correct flags automatically.

> **PreToolUse hook:** The unified hook router intercepts `glab mr create/update` (and the MCP equivalents) and validates title + description against the active overlay's rules **by default** via `t3 tool validate-mr` — no env-var opt-in — **blocking** non-compliant calls before the push with a clear error. The verdict is the same one `t3 <overlay> pr create` enforces. Fix the reported issues and retry — no manual validation needed.

#### Open Questions & Assumptions (Non-Negotiable)

Any open question (solved or not) and any assumption that is not 100% explicit from the spec itself MUST be listed in **both** the git commit message body **and** the PR/MR description, under an `Open questions & assumptions` section. This is the single source of truth for the requirement — the commit-format reference and `code/SKILL.md` point here.

Format: one bullet per item, each tagged with a status:

- `decided-by-user` — was an open question; the user made the call. State the decision.
- `assumed` — an assumption the implementer made because the spec was silent. State what was assumed and why.
- `open` — still unresolved. State the question and the chosen default (if any).

```text
Open questions & assumptions:
- decided-by-user: warn-only on the PR side, no hard-fail (matches the "gate without a reliable heuristic warns" rule).
- assumed: the commit-msg warn is out of scope unless a commit-msg hook chokepoint already exists.
- open: should the heading wording be enforced verbatim? Defaulted to accepting common heading variants.
```

When there is genuinely nothing to surface, the section carries a single `- none` bullet — the section is never silently omitted, so a reviewer can tell "nothing to flag" apart from "the author forgot".

`t3 <overlay> pr create` WARNS (exit 0, never blocks) when the PR body has no `Open questions` heading, with a hint to add the section. The warn is the prompt, not a gate — a reliable bad/legit separation is impossible (the section can be worded many ways), so it warns per the "gate without a reliable heuristic warns" rule. The detector + warn live in `teatree.core.gates.open_questions_gate` and fire from both PR-creation chokepoints (`ShipExecutor._build_pr_spec` and the orphan-branch `create_or_defer_pr`).

### 5b. Multi-Phase PRs Must Name Every Phase in the Title (Non-Negotiable)

When a PR ships work spanning more than one numbered phase of an issue (e.g. `phase 3` of an 8-phase rewrite), the title MUST list every phase the diff covers — not only the lead phase. A reviewer must be able to read the title and know exactly what scope to expect.

- Single phase: `feat(scope): <subject> (#NNN phase 3)` — fine.
- Bundled phases: `feat(scope): <subject> (#NNN phases 1-6 + 8)` — preferred range form.
- Non-contiguous: `feat(scope): <subject> (#NNN phases 2, 3, 8)` — explicit list.

**Why:** A title that says "phase 3" while the diff also demolishes the dashboard (phase 2), adds the statusline file (phase 1), introduces the loop scaffolding (phase 4), and lands the no-leak gate (phase 8) is misleading. Reviewers gate their attention by title; mis-titling forces them to discover scope from the diff, which is the slow path. Past failure: PR #543 advertised "phase 3" but bundled phases 1, 2, 3, 3.6, 4, 5, 6, 8.

**How to apply:** before creating the PR, list the commits and decide which phase each commit belongs to. If the answer covers more than one phase, use the bundled form. The same rule applies when a description says "phase X" — the description's first line must match the title.

### 6. Monitor Pipeline

- Background polling for pipeline status.
- On failure → delegate to fix-push-monitor loop (see `/t3:test`).

**Not-green == red (Non-Negotiable).** When monitoring a pipeline, the *only* acceptable terminal state is every required job `success`. Any job that is **not** `success` — `failed`/`error`, `canceled`, `skipped`, `manual` (not run), `blocked`, an `allow_failure: true` job that is failing, or a gray/unknown state — is a **failure**: find the cause, fix it, re-trigger the job, and confirm it goes green. Never report a pipeline OK while any job is non-green, never "walk away" from a gray/skipped/manual job, and never treat `allow_failure: true` as "safe to ignore" — `allow_failure` keeps the *pipeline* green but the job still failed and must be investigated. A still-running/pending job is not yet a failure — wait for it to reach a terminal state, then apply this rule. (Enforced in the loop's PR scanner: `teatree.loop.scanners.my_prs._needs_attention`.)

### 7. Review Request

- Send notification to the appropriate review channel.
- Only after pipeline is green.

## Addressing Review Comments (Post-PR)

When fixing review comments on an already-existing PR:

0. **Verify branch alignment.** Confirm the worktree is on the PR's source branch (`git branch --show-current` vs PR metadata). If the worktree uses a different branch name, resolve the mismatch **before** editing: either checkout the PR branch or plan to cherry-pick onto it after committing. Discovering the mismatch mid-push wastes time on branch gymnastics.
1. **Fix the issues** as requested.
2. **Merge the default branch** if needed: `git merge origin/main`. **Never rebase** — the branch has already been reviewed.
3. **Run the full local gate set** (`t3 tool verify-gates` — both commit- and push-stage hooks) after merging — merges can expose new lint violations in your code even without conflicts.
4. **Push without squashing or rebasing** (regular commits on top).
5. **Reply to the review comments on the PR.**
6. **Do NOT send a review request notification** — reviewers are already watching.

### Merge-only update: push then verify, no fresh re-review (the canonical HOW)

When an already-reviewed PR's ONLY new commit is the origin merge that brought it up to date (no new logic), a fresh independent re-review is NOT required — CI green on the merge result is the gate. Do X — never Y: DO push the merge result and let CI gate it. NEVER re-dispatch the reviewer, re-fetch the diff for review, or spawn a `t3:reviewer` for a merge-only delta (that re-review is the bottleneck this rule removes).

```bash
# The reviewed branch already has the origin merge committed. Push and let CI verify:
git push
```

Then watch CI (§ 6 Monitor Pipeline). Re-reviewing already-reviewed work just because the default branch advanced is the exact waste this rule cuts; the spawn boundary is only earned by NEW logic, not by a clean merge.

## Merging the Default Branch into a PR (Non-Negotiable)

Before touching the PR branch to "prepare" it for a merge, reason through what a clean 3-way merge would produce on its own:

- **Default branch removed keys/lines the PR still has?** The merge will apply those removals automatically — no preemptive cleanup commit needed. Adding one creates noise and risks side effects (e.g., `json.dumps` round-tripping normalizes unrelated formatting).
- **Both branches independently added the same key/line with different values?** That is a true add/add conflict. But verify the merge result first — the merge may have already resolved it correctly. Only surface it to the user if the result actually needs to change.

**Merge conflict resolution for JSON files:**

- Use proper 3-way semantics: `result = theirs + (ours_keys − base_keys)`. This correctly applies the default branch's removals while keeping the PR's own additions.
- Do NOT use `json.dumps` to serialise back — it normalises indentation and whitespace across the entire file, producing a noisy diff far beyond the intended change. Remove keys surgically (line-by-line) to preserve original formatting.
- Do NOT use `git checkout --ours` on whole files — this discards the default branch's removals and reintroduces whatever it had cleaned up.

**After resolving conflicts, verify before asking anything:**

1. Check that all PR-own additions (keys in ours but not in the merge base) are present in the result.
2. Check that any values that differ between ours and theirs are already at the correct value per the merge strategy. If the result is already correct, do not ask the user — they made no decision to make.

## Isolate Unrelated Fixes (Non-Negotiable)

When a CI failure (or any bug found during work) is **pre-existing** — not introduced by the current branch:

1. **Do NOT fix it on the feature branch.** It pollutes the PR diff and conflates unrelated changes.
2. Create a **dedicated branch** from the default branch (e.g., `<prefix>-myproject-fix-flaky-test-ordering`).
3. Apply the fix there, push, and open a **separate PR** targeting the default branch.
4. Once merged (or while waiting), rebase the feature branch to pick up the fix.

**How to detect:** `git diff origin/main...HEAD --name-only` — if the failing file was never touched by the feature branch, the bug is pre-existing.

## One Open PR Per Ticket (Non-Negotiable)

Before opening a new MR/PR, check whether a sibling PR for the **same ticket** is already open on the same repo:

```bash
gh pr list --repo <repo> --search "<ticket-ref> is:open" --json number,headRefName,baseRefName
```

If a sibling is open, **do not open a second PR targeting the default branch** — the two branches will diverge on the same files and the second one will need a painful 3-way merge. Pick one:

1. **Wait for the sibling to merge**, then rebase the new work on the updated default branch and open the PR.
2. **Stack on the sibling's branch** — set the new PR's base to the sibling's source branch (`gh pr create --base <sibling-branch>`). Update the base to the default branch after the sibling merges, so the stacked PR stays minimal.

**Never open two PRs on the same ticket targeting the default branch in parallel.** The only exception is when the two PRs touch genuinely disjoint files (different repos, different modules with no shared imports, no overlapping generated docs) — and even then, the second PR's description must name the sibling PR it races with.

### Also sweep by content for ticketless PRs (Non-Negotiable)

The ticket-ref query above misses **retro fixes, skill edits, and other PRs without a ticket reference**. Before opening any such PR, also run a content sweep against open and recently-merged PRs on the same repo and look for overlap on title keywords or touched files:

```bash
# Open PRs (parallel work in flight)
gh pr list --repo <repo> --state open --json number,title,headRefName

# Recently-merged PRs (work that landed minutes ago — same risk)
gh pr list --repo <repo> --state merged --limit 10 --json number,title,mergedAt
```

Match against:

- **Title keywords** that overlap with the about-to-be-pushed PR's title (e.g., "rules", "worktree", "anti-fabrication"). Synonyms count.
- **Touched files** that overlap with `git diff --name-only origin/main..HEAD` on the local branch — for skill PRs especially, multiple agents/users converge on the same `skills/<topic>/SKILL.md` file.

Treat a hit on either signal as a sibling and apply the same options (wait, stack, or bundle per `## Bundle Into an Existing Open PR` below). If the hit is in the recently-merged list, run `git fetch origin main && git log origin/main..HEAD` — if the local diff is now empty, abandon the branch instead of pushing an empty PR.

## Bundle Into an Existing Open PR

When a session uncovers a small unique commit on a now-stale branch (typical during cleanup or retro), and opening a dedicated PR for that one commit would be more ceremony than the change deserves, **bundle it into a sibling open PR** instead. This trades a little PR-scope discipline for delivery speed.

**Eligibility — all must hold:**

1. The commit is small and self-contained (single concern, no cross-cutting impact).
2. The target PR is **still open** and **not yet approved** (bundling into an approved PR forces re-review).
3. The target PR is on the same repo and the change is at least loosely thematically adjacent. Strictly unrelated bundles are still better than abandoning the work, but explain it in the PR description.
4. The bundled commit doesn't depend on or contradict anything in the target PR's diff.

**Procedure:**

1. Fetch the target PR's worktree (or create one with `t3 <overlay> workspace ticket <issue-url>` — use the same issue as the target PR).
2. Cherry-pick the commit: `git cherry-pick <sha>`. Resolve any conflicts surgically.
3. Run lint + the affected tests locally.
4. Push to the target PR's branch (regular push, no rebase).
5. **Update the target PR's title and description** to reflect both commits. Title format becomes `type(scope1): X + type(scope2): Y` if the two are heterogeneous. Body explains both fixes.
6. Notify the reviewer in the PR comments that the scope grew, with a one-line rationale.
7. Force-remove the original worktree and delete the now-empty branch (`git worktree remove --force <path>` + `git branch -D <branch>`).

**Anti-pattern:** bundling into a PR that's already passed review. The reviewer's approval covered the original scope, not the bundled commit.

## Rules

- **Verify PR state before claiming merge status.** Check with `gh pr view N --json state` or `glab mr view N --output json` — session memory of merge state drifts as other agents push/merge.
- Untested code must not be pushed. Local verification by the user is mandatory before pushing. If the project requires E2E tests for UI changes, those tests must be **written and green** before pushing — not "pending" or "will do after PR".
- **Never rewrite settled commits (Non-Negotiable).** Never rebase, amend, or force-push commits that are already on origin. This applies always — not just after review. Before any squash/fixup, check `git log origin/<branch>..HEAD` to confirm which commits are local-only. Even within local-only commits, **only squash commits from the current work session** — older commits on the branch that predate the current task are settled history. When the user says "squash what belongs together", ask which commit range is in scope rather than assuming the entire local history is fair game.
- **No rebase / force push after review.** Once a PR has been reviewed, the branch history is shared. Only merge the default branch and push new commits.
- **Cancel stale pipelines** before every push to a branch with an existing PR.
- **Cancel running pipelines when closing an MR/PR.** When a PR is closed (abandoned, superseded, or replaced), cancel any running or pending pipelines for that branch immediately — they waste CI resources on code that will never be merged.
- **Clickable references:** Every PR, ticket, or note reference must be a markdown link — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Clickable References".
- **Commit early, commit often.** Never accumulate more than 1-2 tickets of uncommitted changes. Commit after completing each ticket or logical unit of work. Squash later with `t3 <overlay> workspace finalize`.
- **Prefer `git commit -a`** when committing changes that touch files the linter might reformat. Pre-commit hooks (ruff-format, end-of-file-fixer) modify files and re-stage them. If you stage specific files, the hook may modify OTHER files that remain unstaged. The pre-push hook then stashes these unstaged changes and fails to restore the patch. Use selective staging only when you specifically need to exclude files.
- **Verify static asset URLs** after any change to `<script src>` or `<link href>` in templates: (1) check the URL resolves (`curl -sI <url>`), (2) if vendoring locally, verify file size > 1KB (a 45-byte file is an error page), (3) Playwright screenshot + console error check.
- **Publishing actions are mode-conditional.** Canonical rule: see [`../rules/SKILL.md`](../rules/SKILL.md) § "Publishing Actions Are Mode-Conditional". In `interactive` mode (default) every push/PR/merge/remote-delete needs separate explicit approval. In `auto` mode (DB-home `mode = auto` via `config_setting set mode auto`, or `T3_MODE=auto`) the agent ships end-to-end without confirm prompts; only the always-gated list (force-push to defaults, history rewrites on shared defaults, destructive shared-state ops, unauthorised external writes, `--no-verify`) remains confirm-gated.
- **Merging is the §17.4 keystone transition, not raw `gh`.** Raw `gh pr merge` / `glab mr merge` and the old `t3 <overlay> pr merge` helper are FSM-incoherent (they skip `MergeClear` validation, `expected_head_oid` SHA-binding, the atomic CLEAR-consume + `MergeAudit` + attestation binding + `mark_merged()`) and are **mechanically refused** — `hook_router._BLOCKED_COMMANDS` denies the raw commands and `pr merge` returns a redirect error. The sanctioned path is two `t3` steps, maker != checker throughout:
  1. **The orchestrator (coordinator) issues the per-diff CLEAR** after an independent cold review: `t3 <overlay> ticket clear <pr_id> <slug> --reviewed-sha <sha> --reviewer-identity <independent-reviewer> --blast-class <substrate|logic|docs> [--ticket-id N] [--human-authorize <id>]`. The reviewer identity must not be a maker/coding-agent/loop role and must differ from the executing loop (§17.8 clause 3). This prints a `clear_id`, which the orchestrator passes to the loop.
  2. **The durable review-loop executes it**: `t3 <overlay> ticket merge <clear_id>`. The transition re-reads the CLEAR from the DB, re-verifies the live head SHA == `reviewed_sha`, live required-checks green, not-draft, and binds the GitHub merge to `expected_head_oid` (fail-closed on head drift). The #764 noreply-author guarantee is unchanged — the server-side squash author is the merging account's `users.noreply.github.com` address.

  **Keystone merge recipe (the canonical HOW).** The two `t3` steps, copy-pasteable — do X, never the raw `gh pr merge` / `glab mr merge` (mechanically refused, #863):

  ```bash
  # Step 1 (orchestrator): issue the CLEAR after an independent cold review.
  # --reviewer-identity MUST be the independent reviewer (e.g. codex, claude-cold-review),
  # NEVER self/maker/me — that defeats maker!=checker:
  t3 <overlay> ticket clear <pr_id> <slug> --reviewed-sha <sha> \
      --reviewer-identity <independent-reviewer> --blast-class <substrate|logic|docs> [--ticket-id N]
  # → prints a clear_id

  # Step 2 (durable review-loop): execute the merge with the printed clear_id.
  t3 <overlay> ticket merge <clear_id>

  # Substrate class only: the CLEAR was issued with --human-authorize <owner-id>;
  # the agent then executes the merge presenting that recorded authorization:
  t3 <overlay> ticket merge <clear_id> --human-authorized <owner-id>
  ```

  Do X — never Y: DO issue the CLEAR with `--reviewer-identity <independent-reviewer>`. NEVER cite `--reviewer-identity self` / `maker` / `me`. DO complete a substrate merge with `--human-authorized <owner-id>`; NEVER auto-merge a `blast_class=substrate` CLEAR without it. NEVER reach for `gh pr merge` / `glab mr merge` / `t3 <overlay> pr merge` — all three are blocked.

  **The loop NEVER self-issues its own CLEAR** (§17.8 clause 3 — the reviewer identity must differ from the executing loop); it only ever runs step 2 with a `clear_id` the orchestrator handed it. Raw `gh pr merge` / `glab mr merge` are mechanically prohibited (#863) — only this sanctioned two-step path is valid. On any pre-condition failure the FSM is left untouched and the result is flagged `escalated` (the loop re-escalates to the durable backlog; it never self-issues a replacement CLEAR). Substrate-class CLEARs are never auto-merged — see the substrate approval path below.
- **Substrate PRs require an explicit recorded human approval (`MergeClear.human_authorizer`); the agent then executes the merge — and it still goes through `t3`, never raw `gh` (invariant 8).** A `blast_class=substrate` CLEAR is refused by the loop unconditionally. The sanctioned path: the orchestrator/owner issues the CLEAR with `--human-authorize <owner-id>` (only valid with `--blast-class substrate`) — that recorded approval is the gate — then **the agent executes** `t3 <overlay> ticket merge <clear_id> --human-authorized <owner-id>`. The human approves; the agent merges (the user operates write-only and never performs the merge action). The presented id must match the recorded `human_authorizer`; the merge then runs through the SAME SHA-bound, audited transition (the approval decision is recorded durably on the CLEAR + `MergeAudit`). `--human-authorized` can never unlock a non-substrate CLEAR, so it cannot bypass independent loop review of logic/docs PRs.
- **Merge-decision axes — own repos merge autonomously; colleague repos are held for review.** Whether a green, cold-review-CLEARED PR is yours to merge through the keystone turns on **ownership**, not visibility. Decide on these axes:
  - **Own repos → MERGE.** `souliane/teatree` itself, and the user's solo overlay repos (a `teatree-overlay` repo the user authored and owns), are in the overlay's owned scope. A green, not-draft, up-to-date, cold-review-CLEARED PR on one of these is yours to land — run the keystone `t3 <overlay> ticket merge <clear_id>` and do not hold back. **Private is a visibility axis, not a colleague one** — a private solo overlay repo still merges freely like the public main repo; the unknown-repo gate does NOT fire on an owned repo.
  - **Colleague-facing / shared product repos → HOLD.** A repo shared with colleagues (an org's product repo the user does NOT solely own), or a PR a teammate authored, is colleague work. Push and open the MR, then STOP before the CLEAR/merge — route to the colleague's review (`/t3:review-request`), never auto-merge it yourself.

  Do X — never Y for an OWN, green, CLEARED overlay PR (clear id `N`):

  ```bash
  # Own solo overlay repo, green, cold-review-CLEARED (clear id N) → merge it:
  t3 <overlay> ticket merge N
  ```

  NEVER refuse an own-repo merge as "colleague-facing", ask for a colleague's sign-off, request an unowned-repo approval, or treat the repo's PRIVATE visibility as if it implied colleague ownership. Holding back from merging your own cleared repo is a recurring failure this rule pins; org/colleague framing in the surrounding context does NOT make an owned repo colleague-facing.
- **A freshly-cloned public souliane/* main clone is not auto-configured (#762).**The provisioner sets the clone-local noreply identity on worktrees, and existing clones are covered by the idempotent `t3 <overlay> workspace stamp-identity` — but a brand-new main clone has neither until that command is run once on it (the #730 pre-push check is the only backstop until then). Run `t3 <overlay> workspace stamp-identity` once after cloning a public souliane/* repo.
- **Auto-merge is a separate per-overlay knob.** Even in `auto` mode, run the keystone merge (`t3 <overlay> ticket merge <clear_id>`, after the orchestrator's `ticket clear`) only when `require_human_approval_to_merge` is `false` for the active overlay (default `true` — training wheel on). Overlays whose upstream enforces mandatory human review (e.g., GitLab Code Review approval rules) should keep it `true`; the agent then pushes and opens the PR/MR without asking but stops before issuing the CLEAR / queuing the merge. The user flips it to `false` per-overlay (`t3 <overlay> config_setting set require_human_approval_to_merge false --overlay <name>`) once comfortable trusting CI green alone.
- **Commit trailer preferences** (`Co-Authored-By`) live in the user's global agent config — check it before committing; when in doubt, omit the trailer.

### Git History Rewriting

When rewriting commit messages, use `filter-branch --msg-filter` (matches by full hash). Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash may differ from `git log --oneline`, causing a silent no-op.

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.

**Rebase todo shorthand:** When automating `git rebase -i` with `GIT_SEQUENCE_EDITOR`, the todo list uses single-letter shorthand (`p` not `pick`, `f` not `fixup`). Match on `^p` not `^pick`. Use `sed -e '/^p <hash>/s/^p/f/'` for fixup squashing.
