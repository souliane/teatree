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
│ agent           Launch Claude Code with auto-detected project context.       │
│ sessions        List recent Claude conversation sessions with resume         │
│                 commands.                                                    │
│ cost            Show cycle-to-date SDK-equivalent spend vs the monthly       │
│                 credit.                                                      │
│ speak           Read text aloud through the local speakers per  (no-op       │
│                 unless local = all).                                         │
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
│ loop            Manage the tick-driven autonomous loops. Session-bound by    │
│                 design: they run only while a Claude Code session is open.   │
│                 Under #2650 each enabled DB `Loop` row is its own native     │
│                 Claude `/loop` firing `t3 loops tick --loop <name>` on its   │
│                 own cadence — there is no master tick. Each per-loop tick    │
│                 atomically claims the next pending unit (`t3 loop            │
│                 claim-next`) and spawns one fresh bounded sub-agent for it.  │
│                 There is no roster of long-lived loop sub-agents to re-spawn │
│                 (#786 WS3): if a loop's owner session dies, the next open    │
│                 session claims its slot and keeps ticking; with zero         │
│                 sessions open the loops are paused until the next session    │
│                 start (no OS daemon — accepted, not a defect). A per-agent   │
│                 Stop-hook self-pump re-continues the loop automatically      │
│                 while consolidated work remains — exactly one consolidation  │
│                 loop per agent identity, deduped across all sessions (#786   │
│                 WS4); it idles when none.                                    │
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
│ teatree         Commands for the t3-teatree overlay.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## `t3 loop`

```
Usage: t3 loop [OPTIONS] COMMAND [ARGS]...

 Manage the tick-driven autonomous loops. Session-bound by design: they run
 only while a Claude Code session is open. Under #2650 each enabled DB `Loop`
 row is its own native Claude `/loop` firing `t3 loops tick --loop <name>` on
 its own cadence — there is no master tick. Each per-loop tick atomically
 claims the next pending unit (`t3 loop claim-next`) and spawns one fresh
 bounded sub-agent for it. There is no roster of long-lived loop sub-agents to
 re-spawn (#786 WS3): if a loop's owner session dies, the next open session
 claims its slot and keeps ticking; with zero sessions open the loops are
 paused until the next session start (no OS daemon — accepted, not a defect). A
 per-agent Stop-hook self-pump re-continues the loop automatically while
 consolidated work remains — exactly one consolidation loop per agent identity,
 deduped across all sessions (#786 WS4); it idles when none.

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
│ loop-state     Read a mini-loop's durable state, read-only (ENABLED when     │
│                never touched; no mutation).                                  │
│ claude-spec    Print the native Claude `/loop` spec (slot_id, cron, prompt)  │
│                for one DB Loop.                                              │
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
│                stands down while a real `db_worker` holds the                │
│                `teatree-worker` singleton.                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```
