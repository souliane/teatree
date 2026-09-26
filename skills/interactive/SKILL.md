---
name: interactive
description: "Shared Claude Code and Codex contract for an attended TeaTree session: no work-bearing state is terminal, skills are selected explicitly, and interactive output stays human-readable. Claude Code plugin hooks additionally mark the session engaged; Codex loads this as an ordinary skill and does not emulate those hooks or arm loops. Load it when ending an interactive session, when a session-end report names stranded work, or when deciding what to do with uncommitted, unpushed, untracked or unmerged work. TeaTree's own architecture and coding rules are `t3:internals`; the dogfooding procedure is `t3:dogfooding`."
compatibility: any
requires:
  - rules
eval_exempt: harness-wiring reference plus one invariant that points at the four mechanisms enforcing it deterministically; the engagement behaviour is pinned by tests/test_teatree_opt_in.py and each mechanism by its own tests, not by an agent trajectory
metadata:
  version: 0.0.2
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
from `issue_implementer_label`, falling back to `t3-auto`):

```text
mcp__teatree__github_issue_create(
  title="<what the operator asked for>", body="<detail>", labels=["t3-auto"])
```

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

**Session-end check.** Every session end sweeps all five states and names each item with the exact command that advances it. It runs unconditionally — which skills a session loaded says nothing about whether it stranded work — and it fails open, so a probe that cannot answer contributes nothing rather than breaking the session. It lives in `hooks/scripts/session_end_work_check.py`.

**Aged-skip surfacing.** The merge sweep declines to merge on about ten reasons, all of them sound per tick and all of them silent. A reason that repeats for the same PR across consecutive passes is announced once, naming the PR, the reason and how long it has been held, then re-announced only after a 24h backoff — the backoff is per PR, not per reason, so a stuck PR whose reason wobbles between `ci_red` and `ci_pending` gets a daily reminder rather than a DM per flap; `t3 doctor check` reports every aged hold standing.

### Using it

When a session-end report names stranded work, run the command it prints for each item. The states are ordered, so an item usually needs its own next step and nothing more — commit, push, `t3 teatree pr ensure-pr --branch <name> --repo <absolute-worktree-path>`, or let the ship loop take the PR (`t3 loops tick --loop ship`).

**Add `--repo` yourself — the printed command omits it, and omitting it fails on EXIT 0.** The `.` default is right only for the pre-push hook, which runs in-process on the host with the repo as its cwd. Invoked by hand, `t3` execs into a container whose cwd is the image's own `WORKDIR` and never the host's, so `.` is not a checkout at all and the classification dies:

```text
{'branch': '<name>', 'error': "could not determine sync status of '<name>' in '.': command failed
 (rc=128): git -C . log <name> --not origin/main … fatal: not a git repository"}
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

## Skill Loading

Skill loading is fully explicit — there is no free-text scan of the prompt. Skills load via the runtime's native syntax (`/t3:code` in Claude Code, `$t3:code` in Codex), phase mapping (`t3 agent --phase coding`), ticket status, the transitive `requires:` dependency chain, and cwd/overlay context. `t3 agent` resolves that selection before launching either runtime and injects the selected skill names through the runtime's native context channel.

The `SkillLoadingPolicy` class resolves which skills to load from an explicit phase / ticket-status / cwd-overlay context and expands each root's `requires:` chain transitively.

**Engagement is default-OFF ([#256](https://github.com/souliane/teatree/issues/256)).** Installing either runtime's skills does NOT force teatree onto every session. The engagement markers and loop scheduling described here are Claude-only hook automation: Claude's `InstructionsLoaded` hook writes `<session>.teatree-active` when this skill (or a requiring skill) loads, while `handle_track_skill_usage` writes `<session>.t3-engaged` for any `t3:` skill. Codex has no equivalent plugin-hook adapter today, so loading `$t3:interactive` adopts this contract but does not write either marker, deliver standing directives, or arm loops. That absence is fail-safe: no loop starts merely because Codex can read the skill.

In Claude Code, a fresh session is *not engaged*: the UserPromptSubmit suggester (and the T3 CLI reminder) is suppressed, `<session>.pending` stays empty so the PreToolUse gate never blocks, and SessionStart shows a one-line how-to advisory instead of arming the loop. A session engages when any of: the owner set `[teatree] autoload = true` (or `T3_AUTOLOAD=1`); a teatree-requiring skill loaded; or any `t3:` skill loaded. The cold-hook seam is `hook_router._teatree_engaged` = `_autoload_enabled() OR _teatree_active() OR <session>.t3-engaged`. The two markers differ: `.t3-engaged` engages only the suggester, while loop scheduling gates exclusively on `.teatree-active`. Explicitly running `/t3:interactive` engages a Claude session for the next prompt.

## Standing directives

Three standing rules are re-delivered to an engaged session on their own cadence, because
they were written down in three places and skipped anyway — the failure is context decay,
so the repetition is automated rather than remembered.

| Slot | Cadence | Reaches | What it holds |
|---|---|---|---|
| `standing-golden-rule` | 300s | every attended session, costing no turn | PLAN → IMPLEMENT → COLD REVIEW, and the orchestrate-only boundary: never dispatch an implementing agent on unplanned work, and never implement it yourself. |
| `standing-todo-consolidate` | 1800s | an attended session that drives itself | Every user request is captured as a task, and every OPEN task is drained — closed when durable state already satisfies it; reconcile from durable state first, rescan the transcript only if unaccounted for; then implement the outstanding requests, oldest first. |
| `standing-pr-board` | 600s | ONE attended session per host | Every open PR advances every pass — review, fix, update, or merge via the keystone — promptly, with every merge guard intact. |

The third column is the cost story. A rule that only has to be in context when you next act
rides the turn already happening, so it reaches widest and is never rationed; a rule that
has to drive work with nobody prompting costs a whole turn, so it reaches only a session
that opted into driving itself — and the board is one board per host, not one per session.
That comes to **2 self-woken turns per hour per attended session plus 6 per host**, and
none at all while the active preset pauses the self-pump (the zero-turn rule still arrives).

Read the live text and that budget with `t3 loop directives show` (`--json` for the machine
contract: `{slot_id, cadence_seconds, text, scope, wakes_session}` per directive). The text
is data, not code — an owner edits a directive by creating a `Prompt` row named
`standing-directive:<slot_id>`, versioned like any other prompt, and the cadences are
tunable per slot (`T3_GOLDEN_RULE_CADENCE`, `T3_TODO_CONSOLIDATE_CADENCE`,
`T3_PR_BOARD_CADENCE`, floors 60/600/300).

Switching a slot off:

```bash
t3 loop directives disable standing-pr-board   # one slot off, versioned and reversible
t3 loop directives enable standing-pr-board    # back on, restoring your own text if you had one
t3 loop directives disable --all               # the whole feature off
```

The directives themselves are harness-neutral: teatree owns the text, the cadences, the
scoping rule and the per-slot delivery cost, and each harness supplies its own delivery
adapter over the JSON contract above. They are advisory — repeated prose, not a gate. A
rule that is repeated is one the session still holds; it is not one it cannot break.

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
