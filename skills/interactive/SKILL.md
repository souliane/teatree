---
name: interactive
description: "Shared Claude Code and Codex contract for an attended TeaTree session: no work-bearing state is terminal, skills are selected explicitly, interactive output stays human-readable, and the session watches the factory and handles BLOCKING abnormalities first. Claude Code plugin hooks additionally mark the session engaged; Codex loads this as an ordinary skill and does not emulate those hooks or arm loops. Load it when ending an interactive session, when a session-end report names stranded work, when deciding what to do with uncommitted, unpushed, untracked or unmerged work, or when checking the factory for abnormalities such as agents run without their skills, missing skills or failing tasks. TeaTree's own architecture and coding rules are `t3:internals`; the dogfooding procedure is `t3:dogfooding`."
compatibility: any
requires:
  - rules
eval_exempt: harness-wiring reference plus invariants enforced by deterministic mechanisms, each pinned by its own tests (engagement by tests/test_teatree_opt_in.py); the factory-watch duty is a read-and-route order whose commands and symbols the skill-command and symbol-ref lanes pin, and its detectors are health collectors and doctor checks with their own tests, so it gets a trajectory eval once issue 5069 gives it one pure-read command to anchor on
metadata:
  version: 0.0.3
---

# TeaTree — Interactive Session

The shared Claude Code and Codex side of teatree: how skills reach an attended agent and the one rule that session must not break. Runtime-specific automation is called out explicitly below; reading this skill in Codex does not activate Claude's plugin hooks or start a loop.

## This session does not implement — it files and enqueues (Non-Negotiable)

When the operator asks for something that would change the code, do NOT implement it.
File a ticket for it, get it prioritized, and let the factory pick it up. Report the
ticket back to the operator.

All three parts are the order; dropping any one recreates the failure:

1. **Do not implement.** Not the edit, not the commit, not the push.
2. **File the ticket.** A request that produces no durable row is a dropped request — the
   operator must never have to repeat themselves.
3. **Get it prioritized and enqueued**, so the factory actually reaches it. A
   filed-but-unadmitted issue looks identical to a delivered one from the operator's side
   and is not the same thing at all.

File it through the MCP forge tool, and carry the admit label — the label is what
makes intake reach it, and a filed issue without one is not enqueued (it resolves
from `issue_implementer_label`, falling back to `t3-auto`).

The tool files in TWO calls (#162 Rule 1), because a request that duplicates an open
ticket is a request the factory will work on twice. The first call writes nothing:

```text
mcp__teatree__github_issue_create(
  title="<what the operator asked for>", body="<detail>", labels=["t3-auto"])
  → {"outcome": "judgment_required", "candidates": [...], "snapshot": [...]}
```

Judge every candidate, then call again with that same snapshot:

```text
mcp__teatree__github_issue_create(
  title=..., body=..., labels=["t3-auto"],
  dedupe={"snapshot": <as returned>,
          "decisions": [{"url": ..., "fits": false, "reason": "<why not>"}]})
```

`extended_existing` means the operator's request was appended to a ticket that already
existed — report THAT url; it is a filed request, not a dropped one. `external_conflict`
means a fitting ticket someone else owns: report it and stop rather than filing a rival.

**Reviewing, merging, diagnosing and answering are NOT implementation.** They are this
session's actual job, and they are unaffected: read, search, probe, run tests, reproduce a
failure, review a PR, merge through the keystone, answer a question, file what you find.

**Two exemptions, and only these two.** They are narrow, and naming them is what stops the
order being read as wider than it is:

- **Work already in flight** — an edit, a commit or a push inside a live t3 worktree for a
  ticket the factory already started.
- **The recorded per-action emergency escape** — `[headless-authoring-ok: <reason>]`, for a
  genuine emergency (the factory itself is down). Single-use, recorded, per action.

The deterministic backstop is the PreToolUse authoring gate
(`hooks/scripts/headless_authoring_gate.py`), which refuses an interactive session's edit,
commit or push against a teatree-managed repo. It applies unconditionally — there is no
setting to flip — and carries both exemptions above.

## And it does not investigate inline — a sub-agent does (Non-Negotiable)

The rule above says who WRITES the code. This one says who READS. An attended session
orchestrates: it dispatches, collects, routes, decides, advances the FSM and communicates.
It does not build understanding in its own context.

The distinction is one line: **routing reads a fact; investigation builds understanding.**
A single bounded call answering "what is the state of X" routes, and is always allowed —
`git status`, `docker ps`, a forge PR/MR state read, `t3 … list --json`, `| jq '.field'`, `head -50`,
`sed -n '1,80p'`. A sweep, a parse, or a chain that produces a conclusion is investigation,
and belongs in a sub-agent: `Explore` for a read-only code search, `t3:debugger` to diagnose,
`t3:bughunter` to reproduce, `t3:reviewer` to read a diff. The sub-agent reads the whole
tree; you read one answer.

Context is the orchestrator's scarce resource, and it is what an unbounded read spends.
That is why `run_in_background: true` does not settle this the way it settles a slow
command — a backgrounded sweep still lands in your context the moment you collect it.

The deterministic backstop is the PreToolUse delegation gate
(`hooks/scripts/orchestrator_delegation_gate.py`), which refuses a Bash call whose shape has
no ceiling: a recursive search with no `-m`/`--max-count` and no `| head`, or an output piped
into an interpreter. It reads Bash only, so every dispatch, task write, `SendMessage`,
`AskUserQuestion` and MCP connector call is untouched. A sub-agent is never gated — sweeping
is its job. Escapes: `[delegate-ok: <reason>]` on the one call, and
`t3 <overlay> gate delegation disable` to turn it off.

## If it does code, it plans first (Non-Negotiable)

Whenever this session legitimately writes code under either exemption — the factory is
down, or it is finishing work already in flight — **a plan artifact exists before the first
edit.** This is an order, not a preference.

It is written hard because it is the rule that fails under pressure: an emergency is exactly
when "there is no time to plan" feels true, and that is when hand-editing does its damage.
The emergency path is unreviewed by construction, so the plan is the only checkpoint it has
left. An emergency is not an exception to this.

```bash
t3 <overlay> ticket plan <ticket-id> "<the plan>"   # PlanArtifact; WORK_STARTED → PLAN_RECORDED
```

Scope: this governs CODE CHANGES, not diagnosis. Reading, grepping, probing, running tests
and reproducing a failure need no plan — they are how the plan gets written.

Beyond the plan, reach for the factory's remaining habits by imitation when you are on the
emergency path: route the work to a sub-agent rather than editing inline, let a reviewer who
is not the maker check it, and verify by executing rather than by reading.

## No work-bearing state is terminal

A session does not end with work it authored sitting unmerged and untracked.

Work is *work-bearing* from the moment it exists in the working tree. There are five such states, and none of them is a place work may come to rest:

| State | Rests when |
|---|---|
| unstaged in the working tree | committed |
| staged, uncommitted | committed |
| committed, unpushed | pushed |
| pushed, no PR | a PR exists |
| PR open, unmerged | merged, or closed with a reason |

Every state either advances or leaves a durable record that something else drains. Nothing may exit 0 having observed work and stored nothing.

A dispatched agent's HARNESS worktree (`.claude/worktrees/agent-*`) is auto-cleaned only when the agent leaves it UNCHANGED. One holding uncommitted work is not reclaimed — it survives on one machine's disk and outside teatree's `Worktree` ledger. `workspace emit` now names it (#4579: it unions the ledger with the unregistered checkouts holding work), so it is visible — but visibility is not delivery, and nothing advances it. Dispatch work that must survive into a teatree-managed worktree instead, and push once a result is worth keeping — a remote ref is the only state wholly independent of the local machine.

### The mechanisms

The invariant is not kept by remembering it. Four mechanisms enforce it, each verifiable on its own:

**Durable deferral + drain.** `ensure-pr` runs pre-push, and a branch's FIRST push legitimately has no remote ref to open a PR against. That deferral persists a row carrying the repo, the branch and the PR spec rather than exiting quietly; the `dispatch` loop drains it on a later tick, and a row that ages without draining becomes a `t3 doctor check` failure. Verify: `t3 doctor check`.

**Sweep capture.** A checkout is snapshotted whenever a sweep OBSERVES it — tracked modifications, staged changes and unpushed commits, recorded in the DB rather than only on disk, then aged into a `t3 doctor check` line. Every disposition is covered, KEPT ones included: capturing only before a teardown missed the one disposition that tears nothing down, a row whose ticket is still open, so 75 of 77 registered worktrees held work no surface named. Dirtiness is read with `git status --porcelain` / `git diff HEAD` everywhere it is decided; a bare `git diff` reports zero bytes against a worktree holding only staged work. A checkout this venue cannot resolve is reported as a WRONG VENUE, never as a broken repository — `t3` runs in Docker, so a container-created checkout reads as corrupt from the host. Verify: `t3 doctor check`, `t3 teatree workspace emit`, and `/t3:sweeping-worktrees` for what to do with each emitted item.

**Session-end check.** Every session end sweeps all five states and names each item with the exact command that advances it. Nothing a session-end hook prints reaches anyone, so the report is kept for the next session that starts in the same checkout and delivered to it once, as context, at its start (a report older than two days is dropped). It runs unconditionally — which skills a session loaded says nothing about whether it stranded work — and it fails open, so a probe that cannot answer contributes nothing rather than breaking the session, and leaves the earlier report in place rather than clearing it. It lives in `hooks/scripts/session_end_work_check.py`; the delivery is `hooks/scripts/stranded_work_start.py`.

**Aged-skip surfacing.** The merge sweep declines to merge on about ten reasons, all of them sound per tick and all of them silent. A reason that repeats for the same PR across consecutive passes is announced once, naming the PR, the reason and how long it has been held, then re-announced only after a 24h backoff — the backoff is per PR, not per reason, so a stuck PR whose reason wobbles between `ci_red` and `ci_pending` gets a daily reminder rather than a DM per flap; `t3 doctor check` reports every aged hold standing.

### Using it

When a session starts with a stranded-work report from the one that ended before it, re-check each item (the state may have moved) and run the command it prints for each item still stranded. The states are ordered, so an item usually needs its own next step and nothing more — commit, push, `t3 teatree pr ensure-pr --branch <name> --repo <absolute-worktree-path>`, or let the ship loop take the PR (`t3 loops tick --loop ship`).

**Keep the `--repo` the printed command carries — typing `ensure-pr` by hand without it fails on EXIT 0.** The `.` default is right only for the pre-push hook, which runs with the repo as its cwd. Invoked by hand, `t3` execs into a container whose cwd is the image's own `WORKDIR` and never the host's, so `.` is not a checkout and the command refuses by name:

```text
{'error': "the process cwd '<image WORKDIR>' (no --repo given) is not a git checkout on this filesystem. Pass --repo …"}
```

That is the whole failure — **no PR created, and the process exits 0**. `ensure-pr` is the sole subcommand exempted from the loud-refusal contract, because it runs inside the pre-push hook where reporting and letting the push through is the designed behaviour (`/t3:internals` § `soft_refusal_commands`, #792) — so by hand the refusal reads as success and nothing downstream catches it. Pass the worktree's ABSOLUTE filesystem path; a forge slug (`owner/repo`) is refused up front (#2937).

**`ensure-pr` has no `--base`/`--target` — it opens against the repo's DEFAULT branch.** A target is read from the owning ticket, and an orphan branch (the case this command exists for) has no `Worktree` row to carry one. On a stack that is a silent wrong target rather than an error, so retarget the layer in the same turn you create it — `/t3:ship` § "Stacked Delivery — One Stack Per Repo (Default)" carries the command.

Deleting an item is a decision, not a default: `/t3:sweeping-worktrees` covers salvaging unmerged work to a fresh PR versus deleting something demonstrably shipped. The reaper refuses a dirty checkout for that reason, so a kept worktree is not a finished one.

## A status field is a report, not the state

Three readouts lie in the same direction — they say *finished* while work is live, or *fine* while nothing ran:

- **`ListAgents` reports `completed` for an agent still working.** Confirm against the artifact — the worktree's dirty count, a new commit, the file it was writing.
- **A green pipeline does not prove a lane ran.** Read the collection count. An `allow_failure: true` lane reports green having executed zero tests.
- **A red pipeline does not prove a lane ran either.** When a metadata validator fails first and gates the rest, every real job shows `skipped` — the tests, the lint, the builds. The failure is loud and the *verification silently did not happen*, which is the more dangerous half.

The rule is one line: **a status is evidence about the reporter, not about the thing.** Before repeating one to a person, name what you actually observed — the count, the SHA, the file — or say you read a status and did not confirm it.

## Factory watch — BLOCKING first (Non-Negotiable)

An attended session is the operator's eyes on the factory. It looks for abnormalities — an agent run without its phase's skills, a missing skill, failing tasks — and handles the blocking ones before anything else. Each abnormality, with its tier, the command that answers it and today's detector (or the issue for the missing one), is in [`skills/interactive/references/factory-watch.md`](references/factory-watch.md); reading the health chip itself is [`/t3:health`](../health/SKILL.md).

**When:** at session start, after every compaction, before any status report to the operator, and at least on every `standing-todo-consolidate` delivery.

**The cheapest bounded reads, in order:** `t3 worker status --json`, `t3 <overlay> health show --json`, `t3 loop self-improve status --limit 30`, `t3 loop preset show`, `t3 tokens --cached --json`. `health show` reconciles before it prints, so it writes. A pure-read `--no-reconcile` flag and an exit-coded `--fail-on blocking` are coming in #5069; neither exists yet.

**`t3 doctor check --json` takes minutes, so it is worth its cost when** health is red with no row naming the cause, a `health-collector-failed` row is open, or the watchdog cannot page. Read only its non-OK findings.

**Tiers:**

- **BLOCKING** — delivery or merging has stopped, or the factory works ungoverned or blind: an agent dispatched without its phase's skills, a missing skill, a merge-gating critic starved behind newer tasks (#5051), quota blindness, a red default branch.
- **DEGRADING** — the factory still delivers, but late or with a weaker guard: a shadowed skill, a schedule firing in the wrong timezone (#5053), an override left on past its reason.
- **COSMETIC** — untidy state or a wrong readout that changes no run: a tracked file matching `.gitignore` (#5064), a stale statusline entry.

**BLOCKING preempts the PR board and the todo drain.** Handle each finding in this order:

1. **Find the durable record before filing anything:** a health row, a self-improve firing at the `ticket` rung, or an open issue found through the forge tool's dedupe above. A recurrence EXTENDS that record instead of starting a new one.
2. **Unblock now** whatever is not implementation — review, merge, answer, re-run — per § "This session does not implement".
3. **Notify the owner once, for BLOCKING only,** keyed so a repeat is a no-op: `mcp__teatree__notify_user` with `idempotency_key="factory-watch:<fingerprint>"`, or `t3 <overlay> notify dm '<finding>' --idempotency-key factory-watch:<fingerprint>`. Not `notify send`: it records an unregistered key without delivering it. Skip the DM when the finding has already paged through a registered push signal (`teatree.core.modelkit.dm_channel_policy.PUSH_SIGNALS`).
4. **Never leave a finding silent:** keep a TODO naming its fingerprint, and close it only on a durable record. When no detector raised it, add one with `t3 <overlay> health add '<fingerprint>: <finding>' --critical` (without `--critical` for DEGRADING).

**No intake priority exists yet (#5071).** Intake claims the oldest admissible issue first, so a filed fix waits its turn — tell the operator so. The one lever today is by hand: `t3 <overlay> workspace ticket <url>`, then a planning task through `mcp__teatree__task_create` or `t3 <overlay> tasks create <ticket> --phase planning --reason "…"`. `workspace ticket` also stamps a one-hour external-delivery lease, and the loop dispatches no task on a leased ticket, so even this starts within the hour rather than at once.

## Skill Loading

Skill loading is fully explicit — there is no free-text scan of the prompt. Skills load via the runtime's native syntax (`/t3:code` in Claude Code, `$t3:code` in Codex), phase mapping (`t3 agent --phase coding`), ticket status, the transitive `requires:` dependency chain, and cwd/overlay context. `t3 agent` resolves that selection before launching either runtime and injects the selected skill names through the runtime's native context channel.

The `SkillLoadingPolicy` class resolves which skills to load from an explicit phase / ticket-status / cwd-overlay context and expands each root's `requires:` chain transitively.

**Engagement is default-OFF ([#256](https://github.com/souliane/teatree/issues/256)).** Installing either runtime's skills does NOT force teatree onto every session. The engagement markers described here are Claude-only hook automation: Claude's `InstructionsLoaded` hook writes `<session>.teatree-active` when this skill (or a requiring skill) loads, while `handle_track_skill_usage` writes `<session>.t3-engaged` for any `t3:` skill. Codex has no equivalent plugin-hook adapter today, so loading `$t3:interactive` adopts this contract but does not write either marker or deliver standing directives. That absence is fail-safe: the `t3 worker` runs the loops whichever runtime reads the skill.

In Claude Code, a fresh session is *not engaged*: SessionStart shows a one-line how-to advisory instead of electing the host's attended loop slot. A session engages when the owner sets `[teatree] autoload = true` (or `T3_AUTOLOAD=1`), a teatree-requiring skill loads, or any `t3:` skill loads. `InstructionsLoaded` writes the `.teatree-active` marker the loop-slot election reads; skill usage writes `.t3-engaged` for engagement tracking. Loading `/t3:interactive` writes the engagement marker for later hook events. No teatree hook runs when the owner submits a prompt.

## Standing directives

Three standing rules are re-delivered to an engaged session on their own cadence, because
they were written down in three places and skipped anyway — the failure is context decay,
so the repetition is automated rather than remembered.

| Slot | Cadence | Reaches | What it holds |
|---|---|---|---|
| `standing-golden-rule` | 300s | every attended session | PLAN → IMPLEMENT → COLD REVIEW, and the orchestrate-only boundary: never dispatch an implementing agent on unplanned work, and never implement it yourself. |
| `standing-todo-consolidate` | 1800s | every attended session | Every user request is captured as a task, and every OPEN task is drained — closed when durable state already satisfies it; reconcile from durable state first, rescan the transcript only if unaccounted for; then implement the outstanding requests, oldest first. |
| `standing-pr-board` | 600s | ONE attended session per host | Every open PR advances every pass — review, fix, update, or merge via the keystone — promptly, with every merge guard intact. |

Every slot arrives as context on a turn that is already happening: all three when the
session starts, resumes, clears or compacts, and each again on the first tool call after
your prompt once its own cadence has passed. That comes to **0 self-woken turns**: nothing
wakes the session, and nothing asks it to register a `/loop` or a cron — teatree's worker
runs the loops. The board is one board per host, so only the session that owns the host's
loop slot receives it — N sessions each driving it would mean N cold reviews per PR and two
sub-agents on one branch. While the active preset masks the dispatch loop off, the two
slots that send the session to work are not delivered; the golden rule still arrives.

Read the live text with `t3 loop directives show` (`--json` for the machine contract:
`{slot_id, cadence_seconds, text, scope}` per directive). The text is data, not code — an owner
edits a directive by creating a `Prompt` row named `standing-directive:<slot_id>`, versioned
like any other prompt, and the cadences are tunable per slot in the worker's environment
(`T3_GOLDEN_RULE_CADENCE`, `T3_TODO_CONSOLIDATE_CADENCE`, `T3_PR_BOARD_CADENCE`, floors
60/600/300). The worker republishes them every minute, so an edit or a preset change reaches
the sessions within one; the switches below take effect at once.

Switching a slot off:

```bash
t3 loop directives disable standing-pr-board   # one slot off, versioned and reversible
t3 loop directives enable standing-pr-board    # back on, restoring your own text if you had one
t3 loop directives disable --all               # the whole feature off
```

The directives themselves are harness-neutral: teatree owns the text, the cadences, the
scoping rule and the mode brake, and each harness supplies its own delivery adapter over the JSON contract above.
They are advisory — repeated prose, not a gate. A rule that is repeated is one the session
still holds; it is not one it cannot break.

## Claude-only hook automation

Claude hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for Claude hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When migrating hooks to the plugin, remove the `settings.json` equivalents in the same change to avoid double execution. Codex does not consume this file; shared behavior must live in the skill or CLI seam, never rely silently on a Claude hook.

## Interactive vs Headless Output

The `{"summary":..., "files_modified":...}` JSON result block from `/t3:next` is consumed by the headless pipeline. In interactive sessions it's noise — skip it and only show the text summary.

## Related Skills

Each skill below declares `requires: interactive`, so loading it engages the session too — that is the contract, and `tests/conformance/test_engagement_skill_requires_walk.py` keeps this table and those edges in agreement.

| Skill | When to load |
|-------|--------------|
| `/t3:dogfooding` | Validating a CLI, loop, or statusline change; or self-QA on the loop and statusline — find, file, and fix bugs in one session |

`/t3:wip` is deliberately absent: it is cross-cutting, so working a backlog does not by itself engage teatree.

`/t3:internals` is NOT in this table on purpose: it loads from the CHECKOUT, not from a mode. Teatree's own architecture and management-command rules are needed on a teatree ticket and irrelevant on a customer ticket, so `SkillLoadingPolicy.detect_internals_skill` keys it on the worktree being a teatree checkout — which reaches a headless worker too, where a mode skill never would.
