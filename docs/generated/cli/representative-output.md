# Representative CLI output

Rendered `--help` output of the canonical `t3` commands, captured deterministically
and drift-checked in CI so it stays an always-fresh fixture. This is the curated
front-door complement to the exhaustive [CLI reference](../cli-reference.md);
edit the CLI, not this file.

## `t3`

```
Usage: t3 [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ startoverlay    Create a new TeaTree overlay package.                        │
│ docs            Serve the project documentation with mkdocs.                 │
│ capabilities    List each command's --json support and exit-code contract    │
│                 (front-end discovery).                                       │
│ agent           Launch Claude Code with auto-detected project context.       │
│ sessions        List recent Claude conversation sessions with resume         │
│                 commands.                                                    │
│ cost            Show cycle-to-date SDK-equivalent spend vs the monthly       │
│                 credit.                                                      │
│ tokens          Show per-account Anthropic 5h / weekly token utilization +   │
│                 status.                                                      │
│ speak           Read text aloud through the local speakers per  (no-op       │
│                 unless local = all).                                         │
│ speak-dm        Attach spoken audio to a user DM per  (no-op unless          │
│                 slack/local on).                                             │
│ fast-push       Stage, commit, push, and create-or-update the PR in one      │
│                 leak-gated step.                                             │
│ ui              Browse and run every t3 command in an interactive terminal   │
│                 UI.                                                          │
│ admin           Run the Django admin for the teatree project on a local dev  │
│                 server.                                                      │
│ info            Installation info (bare) and read-only per-ticket artifact   │
│                 discovery.                                                   │
│ config          Configuration and autoloading.                               │
│ banned-terms    Banned-terms backstop scans.                                 │
│ ci              CI pipeline helpers.                                         │
│ codex           Auto-dispatch /codex:review surfaces.                        │
│ review          Code review helpers.                                         │
│ review-request  Batch review requests.                                       │
│ eval            Behavioral eval harness — bare `t3 eval` runs the whole      │
│                 suite; subcommands target one lane.                          │
│ doctor          Smoke-test hooks, imports, services.                         │
│ tool            Standalone utilities.                                        │
│ setup           First-time setup and global skill management.                │
│ update          Sync teatree core and registered overlays to their default   │
│                 branch.                                                      │
│ assess          Codebase health assessment.                                  │
│ overlay         Dev-mode overlay install/uninstall.                          │
│ loop            Manage the tick-driven autonomous loops. Under #1796 / PR-28 │
│                 the singleton `t3 worker` owns the per-loop tick cadence by  │
│                 default (`loop_runner_enabled` ON): it drains durable        │
│                 self-rescheduling loop-timer chains (django-tasks            │
│                 `run_after` rows), one per enabled DB `Loop` row firing `t3  │
│                 loops tick --loop <name>` on its own cadence — there is no   │
│                 master tick, and the DB loops run with no Claude session     │
│                 open (the SessionStart supervisor keeps one worker alive; on │
│                 a headless box start it once from a login profile).          │
│                 `loop_runner_enabled` is the kill-switch — set it false to   │
│                 stop the loops entirely (there is no fallback plane; PR-28   │
│                 retired the native `/loop` cron mirror). Each per-loop tick  │
│                 atomically claims the next pending unit (`t3 loop            │
│                 claim-next`) and spawns one fresh bounded sub-agent for it;  │
│                 a dying worker leaves its Task reclaimable and the next tick │
│                 re-dispatches it. Check the worker with `t3 worker status`;  │
│                 ensure one is running with `t3 worker ensure`.               │
│ goal            Standing verified-green goals (PR-25).                       │
│ worker          The singleton loop-timer worker (#1796 / PR-28). Bare `t3    │
│                 worker` runs it (the cadence owner, default ON via           │
│                 `loop_runner_enabled`). `status` reports the live holder +   │
│                 resolved kill-switch; `ensure` spawns a detached worker iff  │
│                 enabled and the flock is free.                               │
│ loops           Manage DB-configured autonomous loops (#1796).               │
│ mcp             Read-only MCP server exposing teatree's structured search    │
│                 (stdio).                                                     │
│ prompts         Manage and trigger reusable prompts (#2513).                 │
│ teams           Agent-teams master switch. The teams.enabled config key      │
│                 (default off) gates the pane-backed teammate layer; off      │
│                 keeps the classic in-session sub-agent fan-out.              │
│ slack           Slack integration commands.                                  │
│ task            Alias for `t3 <overlay> tasks <sub>` (sub-agent-friendly     │
│                 short form, #1306).                                          │
│ recover         Find (and optionally recover) work stranded by a             │
│                 network-outage death (#1764).                                │
│ dogfood         Overlay-smoke commands — exercise CLI paths so bugs surface  │
│                 in the loop, not in E2E.                                     │
│ identities      Manage the user's trusted forge identities (#1773).          │
│ dream           Idle-time memory-consolidation (dreaming) cron (#1933).      │
│                 Distils recent session feedback into the ConsolidatedMemory  │
│                 DB ledger on a low-frequency schedule, decoupled from the    │
│                 live work loop. `run` is the manual escape hatch; `tick` is  │
│                 the cadence-gated cron entry point.                          │
│ mutation        Scoped mutation testing over high-value safety modules.      │
│ outer           T4 autoresearch outer loop — propose → ratify → implement →  │
│                 measure → keep-only-if-better. Ships QUADRUPLE-OFF (feature  │
│                 flag + disabled loop row + off_live_tick + critic/signal     │
│                 code guards); a full tick is a no-op at defaults.            │
│ directive       Directive-driven self-modification — capture → interpret →   │
│                 human-ratify → implement → configure → verify →              │
│                 keep-or-revert. Ships QUADRUPLE-OFF (feature flag + disabled │
│                 loop row + off_live_tick + critic/signal code guards); a     │
│                 full tick is a no-op at defaults.                            │
│ teatree         Commands for the t3-teatree overlay.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## `t3 loop`

```
Usage: t3 loop [OPTIONS] COMMAND [ARGS]...

 Manage the tick-driven autonomous loops. Under #1796 / PR-28 the singleton `t3
 worker` owns the per-loop tick cadence by default (`loop_runner_enabled` ON):
 it drains durable self-rescheduling loop-timer chains (django-tasks
 `run_after` rows), one per enabled DB `Loop` row firing `t3 loops tick --loop
 <name>` on its own cadence — there is no master tick, and the DB loops run
 with no Claude session open (the SessionStart supervisor keeps one worker
 alive; on a headless box start it once from a login profile).
 `loop_runner_enabled` is the kill-switch — set it false to stop the loops
 entirely (there is no fallback plane; PR-28 retired the native `/loop` cron
 mirror). Each per-loop tick atomically claims the next pending unit (`t3 loop
 claim-next`) and spawns one fresh bounded sub-agent for it; a dying worker
 leaves its Task reclaimable and the next tick re-dispatches it. Check the
 worker with `t3 worker status`; ensure one is running with `t3 worker ensure`.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ tick           Run one user-manual full-scan tick by hand: scan every        │
│                overlay, dispatch, render.                                    │
│ status         Show the loop's last-rendered statusline.                     │
│ pending-spawn  List pending Tasks (read-only probe; legacy — prefer          │
│                ``claim-next``).                                              │
│ spawn-claim    Claim a Task by id (legacy — prefer atomic ``claim-next``).   │
│ start          Spawn a Claude Code session; the t3-master registers each     │
│                enabled loop's ``/loop``.                                     │
│ stop           Print the slot id to stop in the Claude Code session.         │
│ claim          Claim the session-scoped t3-master slot for this Claude       │
│                session (#1073).                                              │
│ owner          Show which session owns the t3-master slot AND this session's │
│                own id (#1073).                                               │
│ whoami         Print this Claude session's own id — what a hand-off ``--to`` │
│                targets.                                                      │
│ release        Release this session's t3-master claim (#1073).               │
│ claim-next     Atomically claim the oldest pending dispatchable Task, then   │
│                emit it.                                                      │
│ list           Print LIVE loop status: each loop's enabled state, cadence,   │
│                last fire, and next tick.                                     │
│ pause          Pause a mini-loop durably (#1913) — survives restart,         │
│                honoured by tick + self-pump.                                 │
│ resume         Resume a paused OR disabled mini-loop — return it to the      │
│                ENABLED state.                                                │
│ disable        Disable a mini-loop durably — the restart-surviving           │
│                kill-switch.                                                  │
│ enable         Enable a disabled mini-loop — return it to the ENABLED state  │
│                (alias of resume).                                            │
│ loop-state     Read a known mini-loop's durable state, read-only (ENABLED    │
│                when never touched; refuses an unknown name).                 │
│ self-improve   Self-improving monitor — scheduled smell detection with a     │
│                tiered action ladder. Runs as its own dedicated `/loop` slot  │
│                on a separate `loop-self-improve` LoopLease so a long         │
│                self-improve cycle never blocks a fast per-loop tick          │
│                (BLUEPRINT § 5.7).                                            │
│ slack-answer   Reactive, token-cheap Slack-answer loop — the third `/loop`   │
│                slot. Runs on a tight cadence (default 20s) in the same       │
│                t3-master session as `t3 loop tick`, on a separate LoopLease  │
│                so a long answer cycle never blocks a fast regular tick.      │
│                Complementary to the inbound prompt-drain, never a            │
│                double-answer (#1014).                                        │
│ drain-queue    Reactive DB-queue drain loop — a `/loop` slot that keeps the  │
│                django-tasks DB queue advancing without an always-on          │
│                `db_worker`. Runs on a tight cadence (default 30s) on the     │
│                `loop-drain-queue` LoopLease: it retires stale READY jobs,    │
│                then drains a bounded batch of the fresh remainder, and       │
│                stands down while a live worker holds either worker           │
│                singleton.                                                    │
│ preset         Named loop-state presets — mode switching (#3159).            │
│ schedule       Weekly preset schedules — the L2 calendar (#3159).            │
╰──────────────────────────────────────────────────────────────────────────────╯
```
