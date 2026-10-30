---
name: interactive
description: "Shared Claude Code and Codex contract for an attended TeaTree session: it carries every duty of `t3:coordinating` (factory watch, unblocking, owner cards, filing and enqueuing, the mailbox) and adds what only a session with a person at the keyboard needs — no work-bearing state is terminal, skills are selected explicitly, interactive output stays human-readable. Claude Code plugin hooks additionally mark the session engaged; Codex loads this as an ordinary skill and does not emulate those hooks or arm loops. Load it when ending an interactive session, when a session-end report names stranded work, when deciding what to do with uncommitted, unpushed, untracked or unmerged work, or when checking the factory for abnormalities such as agents run without their skills, missing skills or failing tasks. TeaTree's own architecture and coding rules are `t3:internals`; the dogfooding procedure is `t3:dogfooding`."
compatibility: any
requires:
  - rules
  - coordinating
eval_exempt: harness-wiring reference plus invariants enforced by deterministic mechanisms, each pinned by its own tests (engagement by tests/test_teatree_opt_in.py, the split from the shared duties by tests/conformance/test_coordinating_skill_split.py); the duties themselves live in t3:coordinating
metadata:
  version: 0.0.4
---

# TeaTree — Interactive Session

The shared Claude Code and Codex side of teatree: how skills reach an attended agent and the one rule that session must not break. Runtime-specific automation is called out explicitly below; reading this skill in Codex does not activate Claude's plugin hooks or start a loop.

An attended session does everything [`/t3:coordinating`](../coordinating/SKILL.md) says: **Load `/t3:coordinating` now** if not already loaded, because the hooks count a `requires:` member as held without injecting its text. This skill states only what is specific to a session with a person at the keyboard.

## What this session adds to the coordinating duties

- **Factory watch timing:** also at session start and on every `standing-todo-consolidate` delivery.
- **Agent status:** `ListAgents` is the agent listing that reports `completed` while the agent still works.
- **Settled cards:** this session may act on settled architecture cards below Rules 0-5. When the facts settle a card without crossing an owner limit, take the sanctioned action, then record the facts and action with `t3 <overlay> questions dismiss <id> --resolver agent:interactive --reason '<facts read, action taken>'`. Tell the owner what was done in one line. Cite an owner-channel basis with `t3 <overlay> questions coordinate <id> --basis <id> '<exact answer>'`.

## Authoring exemptions and the gate backstop

**Two exemptions to the file-and-enqueue order, and only these two.** They are narrow, and naming them is what stops the order being read as wider than it is:

- **Work already in flight** — an edit, a commit or a push inside a live t3 worktree for a
  ticket the factory already started.
- **The recorded per-action emergency escape** — `[gate-escape:headless_authoring: <reason>]`, for a
  genuine emergency (the factory itself is down). Single-use, recorded, per action.

The deterministic backstop is the PreToolUse authoring gate
(`hooks/scripts/headless_authoring_gate.py`), which refuses an interactive session's edit,
commit or push against a teatree-managed repo. It applies unconditionally — there is no
setting to flip — and carries both exemptions above.

## And it does not investigate inline — a sub-agent does (Non-Negotiable)

File-and-enqueue says who WRITES the code. This one says who READS. An attended session
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
`AskUserQuestion` and MCP tool call is untouched. A sub-agent is never gated — sweeping
is its job. Escapes: `[gate-escape:orchestrator_delegation_gate: <reason>]` on the one call, and
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

A session does not end with work it authored sitting unmerged and untracked. The five states and the mechanisms that drain them are in [`skills/coordinating/references/stranded-work.md`](../coordinating/references/stranded-work.md).

A dispatched agent's HARNESS worktree (`.claude/worktrees/agent-*`) is auto-cleaned only when the agent leaves it UNCHANGED. One holding uncommitted work is not reclaimed — it survives on one machine's disk and outside teatree's `Worktree` ledger. `workspace emit` now names it (#4579: it unions the ledger with the unregistered checkouts holding work), so it is visible — but visibility is not delivery, and nothing advances it. Dispatch work that must survive into a teatree-managed worktree instead, and push once a result is worth keeping — a remote ref is the only state wholly independent of the local machine.

**Hand work owns its checkout state (#5238).** A manual hand-recorded plan, review or held decision mints no writing factory task and does not attest checkout changes. Claim occupancy while editing, release it after the work stops, then explicitly attest the reviewed checkout state before allowing a factory writer to resume. Factory retries may re-attest interrupted writer files when ended same-ticket custody, the current claimant, a clear process table, branch and digest rereads prove the handoff; this does not attest hand edits. A `/loop` claim-next → spawn → record-attempt chain holds its checkout as `task:<pk>` in every phase, attests it at record-attempt, then schedules its next phase; any other record counts as hand work. Cancelling a factory task does not release a checkout with live processes; an agent-issued cancel leaves any stop decision to the owner.

**Session-end check.** Every session end sweeps all five states and names each item with the exact command that advances it. Nothing a session-end hook prints reaches anyone, so the report is kept for the next session that starts in the same checkout and delivered to it once, as context, at its start (a report older than two days is dropped). It runs unconditionally — which skills a session loaded says nothing about whether it stranded work — and it fails open, so a probe that cannot answer contributes nothing rather than breaking the session, and leaves the earlier report in place rather than clearing it. It lives in `hooks/scripts/session_end_work_check.py`; the delivery is `hooks/scripts/stranded_work_start.py`.

When a session starts with a stranded-work report from the one that ended before it, re-check each item (the state may have moved) and run the command it prints for each item still stranded.

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
| `standing-pr-board` | 600s | ONE attended session per host | The coordinating duty § "Unblock — review, merge, advance the board", delivered on a cadence. |

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

## Reach a running factory agent

When a running factory task needs something only this session has — a page or document its credentials cannot read, a correction, a decision — hand it to the running agent. Never re-run its work here.

1. `t3 <overlay> live list` — the sessions running now on this host's workers, each with its task id and `steerable` flag.
2. `t3 <overlay> live inspect <task>` — PASSIVE: state, phase, open tool and progress; the agent is not contacted.
3. `t3 <overlay> live steer <task> --text "<input>"` — ACTIVE: the text enters the agent's current turn at its next tool boundary or turn end.
4. `t3 <overlay> live follow-up <task> --text "<input>"` — ACTIVE: an idle `claude_sdk` session takes one new turn during its 60-second window, up to four turns per session.
5. `t3 <overlay> live pause <task>` — ACTIVE: interrupts the running turn and parks the task with its conversation; the receipt is `paused`, and a Slack `resume` in the ticket's thread releases the same task.

Commands carry the selected session's attempt and worker generation. `stale_command` means that attempt or worker generation changed before delivery; inspect the current live session before sending a new command.

To hand over a resource, paste it (at most 16 KiB) or write it into the task's worktree and steer with the path.

Read the receipt, not the hope. `accepted_current_turn` and `accepted_new_turn` (exit 0) mean the session took the input — not that the model will follow it. `rejected` (exit 3) names why: `turn_ended`, `not_idle` (use current-turn steer), `stale_command` (inspect the new attempt), `not_accepted_in_time` (withdrawn, never delivered later), `backpressure`, `too_large`, `not_steerable`, `duplicate_mismatch`. `unknown_delivery` (exit 4) means the answer was lost: inspect first, and resend only with the same `--command-id`, which returns the first receipt rather than delivering twice. Exit 5 means no worker runs the task or its session has closed: use the durable route, `mcp__teatree__question_answer` (CLI fallback: `t3 <overlay> questions answer`) or the task list. An idle session lists `steerable: false`; use `live follow-up` while its idle window is open. The same commands serve a Codex `$t3:interactive` session; a factory agent cannot run them.

The plugin's `t3 mcp serve --interactive` child joins a live worker as this attended session's peer, which gives it the mailbox tools of § "Mailbox, both ways" in the coordinating skill. The worker revokes this address when the MCP child disconnects; a restarted worker has no old mailbox. When no live worker is online, the plugin keeps its other MCP tools but has no mailbox tools.

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
