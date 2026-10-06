# Factory watch — the abnormality map

One row per abnormality an attended session watches for: its tier, the command that answers it, and the deterministic detector that raises it today. `no detector — #NNNN` names the issue that adds the missing one. The tiers, the read order and the handling order are in [`skills/interactive/SKILL.md`](../SKILL.md) § "Factory watch — BLOCKING first".

| Abnormality | Tier | Answered by | Detector today |
|---|---|---|---|
| Agent dispatched without its phase's skills | BLOCKING | `t3 loop self-improve status --limit 30` (`skill_assurance_gap`); `t3 <overlay> health show --json` (`failed-tasks`) | The dispatch refusal `teatree.agents.skill_assurance.SkillDispatchError` stops the run before a billable turn and records the failed attempt; the `skill_assurance_gap` detector in `src/teatree/loop/self_improve/detectors/skill_assurance_gap.py` escalates repeats to a ticket |
| Skill missing on the box | BLOCKING | `t3 doctor check --json` | Doctor FAIL from `checks_skill_supply._check_dispatched_overlay_skills` (an overlay dispatches a skill that is not installed) and `checks_resources._check_worker_skills_present` (the worker's skills plugin is not registered, so its agents run skill-less) |
| Installed skill older than its source | DEGRADING | `t3 doctor check --json` | Doctor FAIL from `checks_skill_supply._check_skill_source_drift` |
| Shadowed skill: an older copy in an earlier skills dir wins | DEGRADING | no command yet; compare the copies under each harness skills dir | no detector — #5070 |
| Tasks failing | DEGRADING | `t3 <overlay> health show --json` (`failed-tasks`); `t3 loop self-improve status --limit 30` (`lifecycle_incident`, per failure kind) | `operational_health._failed_task_signals` |
| Every recent task dying in the harness | BLOCKING | `t3 <overlay> health show --json` (`consecutive-harness-crashes`) | `operational_health._consecutive_harness_crash_signals` |
| Backlog stranded with nothing dispatching | BLOCKING | `t3 worker status --json` (non-zero exit, `stale` list); `t3 <overlay> health show --json` (`stalled-backlog`); `t3 doctor check --json` (queue-stall FAIL) | `operational_health._stalled_backlog_signals`; the doctor FAIL from `checks_admission_pressure._check_queue_stall` and the `dispatch_gap` self-improve detector, which share `teatree.core.factory.queue_stall.read_queue_stall` |
| A loop not ticking | DEGRADING | `t3 worker status --json` (`stale`); `t3 <overlay> health show --json` (`stale-tick:<loop>`) | `operational_health._stale_tick_signals` |
| Admission pressure | BLOCKING when `critical`, else DEGRADING | `t3 <overlay> health show --json` (`admission-pressure:<lane>`) | `operational_health._admission_pressure_signals` |
| Harness or provider drift | BLOCKING | `t3 <overlay> health show --json` (`harness-provider-drift:<scope>`) | `operational_health._harness_provider_consistency_signals` |
| Disk reclaim stalled | BLOCKING | `t3 <overlay> health show --json` (`reclaim-stalled:disk`) | `operational_health._reclaim_stall_signals` |
| A health source unreadable, so the chip is blind | BLOCKING | `t3 <overlay> health show --json` (`health-collector-failed:<source>`), then `t3 doctor check --json` | `operational_health._unread_source_signals` |
| A doctor FAIL that reached nobody but the watchdog | BLOCKING | `t3 doctor check --json` | The watchdog DMs it; no health row yet — #5069 |
| Merge-gating critic starved behind newer tasks | BLOCKING | `mcp__teatree__task_list` or `t3 <overlay> tasks list --status pending --json` (the oldest pending critic or reviewing task) | no detector — #5070; the admission fix is #5051 |
| Quota blindness | BLOCKING | `t3 tokens --cached --json` (a `healthy` status with a null `utilization_5h` and an old `checked_at`) | no detector — #5070 |
| Red default branch | BLOCKING | `t3 ci fetch-errors main` | Partial only: `teatree.loop.red_set_surface` DMs `main-red` when two or more open PRs inherit main's failing checks, and `teatree.loop.scanners.self_update_ci` holds the self-update pull on a red main without paging. A full detector — #5070 |
| Two live tasks for one ticket and phase | DEGRADING | `mcp__teatree__task_list` or `t3 <overlay> tasks list --json` | no detector — #5070 |
| Override left on past its reason | DEGRADING | `t3 loop preset show` (`[manual]` rows and their reasons); `t3 loop directives show` | no detector — #5070 |
| Schedule firing in the wrong timezone | DEGRADING | `t3 loop preset show` (the active slot's `until` against the operator's clock) | no detector — #5053 |
| A cleared PR left unmerged | DEGRADING | `t3 loop self-improve status --limit 30` (`forgotten_merge`) | The `forgotten_merge` detector; its firing reaches the health chip once #5069 lifts it |
| Owner-prioritised issue waiting behind older ones | DEGRADING | nothing to read — intake has no priority | no detector — #5071 |
| Tracked file matching `.gitignore` | COSMETIC | `git ls-files -ci --exclude-standard` | The `refuse-tracked-ignored-files` prek hook (`scripts/hooks/refuse-tracked-ignored-files.sh`) at commit, and the `tracked-ignored-files` CI job |
| Stale statusline entry | COSMETIC | `t3 loop self-improve status --limit 30` (`stale_statusline_entry`) | The `stale_statusline_entry` detector |

`t3 <overlay> health show` reconciles before it prints, so every read above that goes through it writes. The pure-read form and the exit-coded BLOCKING check arrive with #5069.
