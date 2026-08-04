---
name: interactive
description: "ENGAGES TEATREE FOR THE SESSION, and holds the standing rule that no work-bearing state is terminal. Loading this skill — or any skill declaring `requires: interactive` — writes the `.teatree-active` marker, one of the two conditions in `_loop_auto_load_active()` that arm the loop and statusline (#256); a session that never loads it stays unengaged, by design. Also holds teatree's Claude Code harness wiring: how skills are selected, how plugin hooks are registered, and which output belongs to the headless pipeline. Load it when ending an interactive session, when a session-end report names stranded work, or when deciding what to do with uncommitted, unpushed, untracked or unmerged work. Teatree's own architecture and coding rules are `/t3:internals`; the dogfooding procedure is `/t3:dogfooding`."
compatibility: any
requires:
  - rules
eval_exempt: harness-wiring reference plus one invariant that points at the four mechanisms enforcing it deterministically; the engagement behaviour is pinned by tests/test_teatree_opt_in.py and each mechanism by its own tests, not by an agent trajectory
metadata:
  version: 0.0.1
  subagent_safe: false
---

# TeaTree — Interactive Session

The Claude Code side of teatree: what engages a session, how skills reach an agent, how hooks are registered — and the one rule an attended session must not break. Loading this skill is itself the engagement act.

Not `subagent_safe`: loading it writes the `.teatree-active` engagement marker and its procedures run `t3` commands, so it is not the pure methodology that flag is reserved for.

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

A dispatched agent's HARNESS worktree (`.claude/worktrees/agent-*`) is auto-cleaned only when the agent leaves it UNCHANGED. One holding uncommitted work is not reclaimed — it survives on one machine's disk and outside teatree's `Worktree` ledger, so `workspace emit` never surfaces it and nothing advances it. Dispatch work that must survive into a teatree-managed worktree instead, and push once a result is worth keeping — a remote ref is the only state wholly independent of the local machine.

### The mechanisms

The invariant is not kept by remembering it. Four mechanisms enforce it, each verifiable on its own:

**Durable deferral + drain.** `ensure-pr` runs pre-push, and a branch's FIRST push legitimately has no remote ref to open a PR against. That deferral persists a row carrying the repo, the branch and the PR spec rather than exiting quietly; the `dispatch` loop drains it on a later tick, and a row that ages without draining becomes a `t3 doctor check` failure. Verify: `t3 doctor check`.

**Teardown capture.** A checkout is snapshotted before it can be reaped — tracked modifications, staged changes and unpushed commits, recorded in the DB rather than only on disk. Dirtiness is read with `git status --porcelain` / `git diff HEAD` everywhere it is decided; a bare `git diff` reports zero bytes against a worktree holding only staged work. Verify: `t3 teatree workspace emit`, and `/t3:sweeping-worktrees` for what to do with each emitted item.

**Session-end check.** Every session end sweeps all five states and names each item with the exact command that advances it. It runs unconditionally — which skills a session loaded says nothing about whether it stranded work — and it fails open, so a probe that cannot answer contributes nothing rather than breaking the session. It lives in `hooks/scripts/session_end_work_check.py`.

**Aged-skip surfacing.** The merge sweep declines to merge on about ten reasons, all of them sound per tick and all of them silent. A reason that repeats for the same PR across consecutive passes is announced once, naming the PR, the reason and how long it has been held, then re-announced only after a 24h backoff — the backoff is per PR, not per reason, so a stuck PR whose reason wobbles between `ci_red` and `ci_pending` gets a daily reminder rather than a DM per flap; `t3 doctor check` reports every aged hold standing.

### Using it

When a session-end report names stranded work, run the command it prints for each item. The states are ordered, so an item usually needs its own next step and nothing more — commit, push, `t3 teatree pr ensure-pr --branch <name>`, or let the ship loop take the PR (`t3 loops tick --loop ship`).

Deleting an item is a decision, not a default: `/t3:sweeping-worktrees` covers salvaging unmerged work to a fresh PR versus deleting something demonstrably shipped. The reaper refuses a dirty checkout for that reason, so a kept worktree is not a finished one.

## Skill Loading

Skill loading is fully explicit — there is no free-text scan of the prompt. Skills load via slash commands (`/t3:code`), phase mapping (`t3 agent --phase coding`), ticket status, the transitive `requires:` dependency chain, and cwd/overlay context. TeaTree's UserPromptSubmit hook surfaces only the skills a prompt's cwd/overlay context implies — framework skills (`ac-django`/`ac-python`), the active overlay's own skill, and its `companion_skills`. A PreToolUse hook blocks Python code edits until those load.

The `SkillLoadingPolicy` class resolves which skills to load from an explicit phase / ticket-status / cwd-overlay context and expands each root's `requires:` chain transitively.

**Engagement is default-OFF ([#256](https://github.com/souliane/teatree/issues/256)).** Installing the plugin does NOT force teatree onto every session. A fresh session is *not engaged*: the UserPromptSubmit suggester (and the T3 CLI reminder) is suppressed, `<session>.pending` stays empty so the PreToolUse gate never blocks, and SessionStart shows a one-line how-to advisory instead of arming the loop. A session engages teatree when any of: the owner set `[teatree] autoload = true` (or `T3_AUTOLOAD=1`); a teatree-requiring skill loaded (the `<session>.teatree-active` marker); or **any** `t3:` skill loaded (the `<session>.t3-engaged` marker, set by `handle_track_skill_usage`). The cold-hook seam is `hook_router._teatree_engaged` = `_autoload_enabled() OR _teatree_active() OR <session>.t3-engaged`. Note the two markers differ: `.t3-engaged` engages only the suggester, while loop scheduling still gates exclusively on `.teatree-active` (so a plain lifecycle skill never arms loops). Explicitly running `/t3:interactive` engages the session for the next prompt.

## Standing directives

Three standing rules are re-delivered to an engaged session on their own cadence, because
they were written down in three places and skipped anyway — the failure is context decay,
so the repetition is automated rather than remembered.

| Slot | Cadence | Reaches | What it holds |
|---|---|---|---|
| `standing-golden-rule` | 300s | every attended session, costing no turn | PLAN → IMPLEMENT → COLD REVIEW, and the orchestrate-only boundary: never dispatch an implementing agent on unplanned work, and never implement it yourself. |
| `standing-todo-consolidate` | 1800s | an attended session that drives itself | Every user request is captured as a task; reconcile from durable state first, rescan the transcript only if something is unaccounted for; then implement the outstanding requests, oldest first. |
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

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When migrating hooks to the plugin, remove the `settings.json` equivalents in the same change to avoid double execution.

## Interactive vs Headless Output

The `{"summary":..., "files_modified":...}` JSON result block from `/t3:next` is consumed by the headless pipeline. In interactive sessions it's noise — skip it and only show the text summary.

## Related Skills

Each skill below declares `requires: interactive`, so loading it engages the session too — that is the contract, and `tests/conformance/test_engagement_skill_requires_walk.py` keeps this table and those edges in agreement.

| Skill | When to load |
|-------|--------------|
| `/t3:dogfooding` | Validating a CLI, loop, or statusline change; or self-QA on the loop and statusline — find, file, and fix bugs in one session |

`/t3:wip` is deliberately absent: it is cross-cutting, so working a backlog does not by itself engage teatree.

`/t3:internals` is NOT in this table on purpose: it loads from the CHECKOUT, not from a mode. Teatree's own architecture and management-command rules are needed on a teatree ticket and irrelevant on a customer ticket, so `SkillLoadingPolicy.detect_internals_skill` keys it on the worktree being a teatree checkout — which reaches a headless worker too, where a mode skill never would.
