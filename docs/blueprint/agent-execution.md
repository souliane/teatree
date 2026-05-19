# BLUEPRINT Appendix — Agent Execution

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §5 (§5.1–§5.5 + §5.7–§5.8). §5.6 Loop Topology stays inline in the top-level. Consumer cross-references such as `BLUEPRINT §5.2`, `§5.4`, `§5.7` resolve here.

## 5. Agent Execution

### 5.1 Structured Result Schema

Agents return JSON matching `AgentResult`:

```python
{
    "summary": str,              # One-line summary
    "files_modified": [{         # Files changed
        "path": str,
        "action": "created"|"modified"|"deleted",
        "lines_added": int,
        "lines_removed": int,
    }],
    "tests_run": [{              # Test results
        "name": str,
        "passed": bool,
        "duration_seconds": float,
        "error": str,
    }],
    "tests_passed": int,
    "tests_failed": int,
    "decisions": [str],          # Design decisions made
    "needs_user_input": bool,    # Triggers interactive followup
    "user_input_reason": str,    # Why human input is needed
    "next_steps": [str],         # Suggested follow-up actions
    "commands_executed": [str],  # Shell commands run
}
```

Schema enforces `additionalProperties: false`. Validation is done without jsonschema library (minimal dependency).

### 5.2 Headless Execution (headless.py)

Runs `claude -p <prompt> --append-system-prompt <context> --output-format json`. Used by the `pending_tasks` scanner (§ 5.6) and by direct calls from the lifecycle FSM workers when a phase task is queued. Kept deliberately small — the swap point for an Anthropic SDK runtime when direct-API execution is desired.

**Flow:**

1. Resolve skill bundle for the task's phase
2. Build task prompt (ticket context, PR metadata, work instructions)
3. Build system context (task ID, skills to load, phase-specific instructions)
4. Execute subprocess, capture stdout/stderr
5. Parse JSON result: `_parse_cli_envelope()` extracts `{session_id, result}` from Claude CLI output
6. `_parse_result()` searches reversed output lines for first `{` (allows progress text before final JSON)
7. Validate result against schema
8. Create TaskAttempt with result, exit_code, agent_session_id
9. Call `task.complete()` which triggers automatic ticket advancement

**Model tiering (#880, #562 §3):** `resolve_phase_model(phase)` (in `agents/model_tiering.py`) maps the task's phase to a Claude model tier. Mechanical phases are downgraded by default (`reviewing`/`testing`/`shipping` → `sonnet`, `retrospecting` → `haiku`); reasoning phases (`coding`, `debugging`) and unmapped phases return `None`, so no `--model` flag is added and the user's default model applies. Overridable per phase via `~/.teatree.toml`:

```toml
[agent]
phase_models.reviewing = "opus"   # pin a phase back to the reasoning tier
phase_models.coding = "sonnet"    # opt a reasoning phase into a cheap tier
phase_models.testing = ""         # opt out — inherit the user's default
```

**Auth:** Uses the `claude` binary (Claude Code session auth — no API key required).

**Stuck-loop / cost-spike watchdog (#882).** The agent runs over `Popen` (via `teatree.utils.run.spawn`) so the heartbeat thread can terminate a runaway mid-flight. On every heartbeat tick `LoopWatchdog.breach_reason()` evaluates the task's wall-clock runtime plus the accumulated `TaskAttempt.num_turns` / `cost_usd` deltas (sampled once on the main thread before the subprocess starts — prior-attempt totals are static for the run). On a ceiling breach the subprocess is killed and a `stuck_loop` `TaskAttempt` failure is recorded with the observed deltas (`task.fail()` runs). The conservative default is a 3h runtime ceiling that only trips on a genuinely runaway subprocess; absolute turn/cost budget caps are deferred to #398-4, so those dimensions default off (`0` = disabled). Overridable via Django settings:

```python
TEATREE_LOOP_WATCHDOG = {
    "max_runtime_seconds": 10800,  # 0 = disabled
    "max_turns": 0,                # 0 = disabled
    "max_cost_usd": 0.0,           # 0 = disabled
}
```

**Per-ticket cost cap (#885 / #398-4).** Where the watchdog above bounds a single in-flight subprocess (it kills a runaway mid-run), `TicketBudget` bounds the *whole ticket's lifetime spend* at dispatch time. Before a task's subprocess is launched, `run_headless` sums `TaskAttempt.cost_usd` across every task under the task's ticket; once the cumulative spend crosses the configured ceiling the subprocess is not launched and a `budget_exceeded` `TaskAttempt` failure is recorded (`task.fail()` runs), surfacing the breach on the failure record so a pathological ticket stops draining budget in unattended batch runs. The conservative default mirrors #882's precedent — the cap is opt-in (`0.0` = disabled), so the consumer changes no behaviour until a ceiling is configured. Overridable via Django settings:

```python
TEATREE_TICKET_BUDGET = {
    "max_cost_usd": 0.0,  # 0 = disabled
}
```

### 5.3 Prompt Building (prompt.py)

**`build_task_prompt(task)`** — Work instructions for the agent:

- Ticket context: number, issue URL, title, labels, phase, execution reason
- PR context: open PRs with URL, title, draft status, pipeline status
- Instructions: check progress → identify remaining work → proceed → request input if blocked → run tests

**`build_system_context(task, skills=[])`** — System prompt for headless agents:

- Task/ticket identifiers, skill loading directives
- Phase-specific instructions (reviewing: thorough code review + /t3:next)
- Mandatory post-execution: run /t3:next for retro + structured result + pipeline handoff
- Fallback JSON schema if /t3:next not available

**`build_interactive_context(task, skills=[])`** — System prompt for interactive sessions:

- Same content as system context, plus user-aware instructions
- **First-message acknowledgement (mandatory):** The agent must begin by stating the project, ticket, current state, and planned next steps
- "Before ending, run /t3:next"

### 5.4 Skill Bundle Resolution (skill_bundle.py)

Resolves which skills to load for a given phase:

1. Look up phase in skill delegation map (§9)
2. Add overlay's companion skills from `get_skill_metadata()`
3. Parse each skill's `requires:` frontmatter field
4. Topological sort for correct load order
5. Return list of skill paths

### 5.5 Skill Delegation Map (skill_map.py)

Default mapping from phase to companion skills loaded alongside overlay skills:

```python
{
    "coding": ("test-driven-development", "verification-before-completion"),
    "debugging": ("systematic-debugging", "verification-before-completion"),
    "reviewing": ("requesting-code-review", "verification-before-completion"),
    "shipping": ("finishing-a-development-branch", "verification-before-completion"),
    "ticket-intake": ("writing-plans",),
}
```

Can be overridden via a markdown file at `references/skill-delegation.md` with `## phase` sections and `- skill-name` lists.

<a id="section-5-7"></a>

### 5.7 Self-improving monitor (`/loop` Phase 1)

The self-improve monitor (`teatree.loop.self_improve`) is a **detector swarm** that rides the same tick the regular `/loop` runs. It watches for "smells" the rest of the loop cannot self-report — dispatcher silently skipping a phase, a `MergeClear` issued but never reconciled, a statusline entry whose evidence has gone stale — and converts each into a `SelfImproveFiring` row plus a graduated action. It is the legibility substrate the §§ 17.4–17.8 orchestrator-keystone relies on; without it, a wrong-but-confident loop is unobservable.

**Detector → Firing → Action.** Each detector implements the `SelfImproveDetector` protocol (`src/teatree/loop/self_improve/detectors/base.py`): a `scan() -> list[ScanSignal]` for the rendering layer plus a `detect() -> list[DetectorReport]` that carries the dedup contract. A `DetectorReport` is a frozen dataclass of `(detector, dedup_key, state_hash, severity, max_rung, summary, payload, auto_fix)`. The schedule module (`schedule.py`) takes the reports, looks up the existing `SelfImproveFiring` row for `(detector, dedup_key)`, and decides via `fresh_or_escalated(report, firing)` whether to fire fresh (no row), hold (same `state_hash`), or escalate one rung (`state_hash` changed).

**Action ladder (5 rungs, monotonic).** `log → statusline → slack → ticket → auto_fix`. Each detector declares a `max_rung` ceiling: a Phase 1 dispatcher-gap detector caps at `slack`; a forgotten-`MergeClear` detector caps at `ticket`; only the stale-statusline detector is permitted to climb to `auto_fix`. The ladder advances at most one rung per cycle; rungs never regress. The string constants live on `ActionRung` in `detectors/base.py` with a `_RUNG_CHOICES` drift guard so they cannot diverge from `SelfImproveFiring.Action`.

**Dedup + cool-down.** `dedup_key` is the canonical identity (`teatree.loop.self_improve.dedup.canonical_key`); same key + same `state_hash` is suppressed by cool-down so a chronic smell does not spam every cycle. Re-fire only when the evidence changes (different `state_hash`) — the schedule module then advances one rung, never more. The model carries a `UniqueConstraint(detector, dedup_key)` so the dedup invariant survives a process restart.

**Cost-tiered scheduling.** `run_tier(tier: str)` dispatches the detector registry (`detectors/registry.ALL_PHASE_1_DETECTORS`) filtered by tier — `cheap` (Phase 1: pure DB / file-mtime reads), `medium` (Phase 2: subprocess `git`/`gh`), `expensive` (Phase 3: LLM judgment). Phase 1 ships `cheap` only; the dispatcher contract is stable so Phase 2/3 plug in without a schema change.

**Pre-cycle budget gate (§ 5.7.1).** `precheck_budget()` in `budget.py` runs before any detector and skips the cycle on any failing guardrail, in order: RAM ≥ 85 % used → spawn cap (>3 self-improve firings in the trailing hour) → classifier-denial cool-down (≥3 denials in the trailing hour) → token-budget exhaustion (`T3_SELF_IMPROVE_TOKEN_BUDGET` env). A skip is a dim one-line statusline note (never a Slack DM) carrying the structured reason. The probe is platform-native (`sysctl`/`vm_stat` on macOS, `/proc/meminfo` on Linux) so a missing optional library never crashes the cycle; tests inject the percent directly via `ram_used_percent`.

**Auto-fix whitelist (hard).** A structural test (`test_no_auto_fix_outside_whitelist`) asserts that exactly two detectors carry `auto_fix = True`: `StaleStatuslineEntryDetector` (re-render) and `WorktreeCleanupCandidateDetector` (clean a merged worktree). Phase 1 ships only the first. Any other detector that flips `auto_fix = True` fails the structural test — `auto_fix` cannot leak by accident. This whitelist is the load-bearing safety control on top of the action ladder; auto-merge of substrate work is not on it and never will be.

**Global Slack cap.** The `slack` rung consults a global rate limit of one self-improve DM per 30 minutes across all detectors, so a busy cycle cannot displace the regular review-request notifications. When the cap is hit, the cycle still escalates the firing row (the ladder advances) but the DM is suppressed and recorded as `slack_capped` on the action result.

**Singleton + loop-owner gate.** `loop_self_improve` acquires a dedicated `LoopLease("loop-self-improve")` (separate from the regular `loop-tick` lease) so a long self-improve cycle never blocks a fast regular tick. The mgmt command refuses to run when the current session is not the loop owner (reads the same `loop-registry.json` `t3-loop-tick-owner` record `hook_router` writes at `SessionStart`); manual CLI invocations outside a session bypass the gate.

**#1107 Prong B tick piggyback.** `teatree.loop.tick_piggyback.run_piggyback_cycles`, called from `loop_tick.Command.handle` on the **won-owner success path only** (after the `loop-tick` lease `finally`, after the #1073 owner gate — a non-owner foreign-session SKIP never reaches it, anti-#1073-hijack), runs one cheap-tier `run_tier(Tier.CHEAP)` behind the **same** `LoopLease("loop-self-improve")` CAS the dedicated slot uses. If a real dedicated slot holds the lease the piggyback's CAS loses and it skips, so the dedicated fast path is never double-run; the lease is acquired with a per-tick-unique owner and `lease_seconds=<T3_SELF_IMPROVE_CHEAP_CADENCE>` and is **never released** by the piggyback, so the lease TTL doubles as the throttle (a re-tick inside the cadence window loses the CAS). This is the defense-in-depth that keeps self-improve running even in a pure-cron / no-session deployment where Prong A cannot resolve a loop owner — zero new state, zero new columns.

**Hard non-goals.** The self-improve monitor never auto-merges substrate, never bypasses the § 17.4 `MergeClear` reviewer-attestation requirement (the keystone), and never auto-edits memory / skills / `BLUEPRINT.md`. The auto-fix whitelist is the structural enforcement of these non-goals — anything outside the whitelist surfaces via statusline / Slack / ticket and waits for a human.

**Cross-references.** The keystone the monitor underwrites is §§ 17.4–17.8 (`MergeClear` + reviewer-attestation + orchestrator legibility). The regular tick topology this rides on is § 5.6. The rendering surface is § 5.6.1. The mode + training-wheel gating the action ladder respects is § 5.6.2.

### 5.8 Reactive Slack-answer loop (`/loop` Phase 2 — the third slot)

The reactive Slack-answer loop (`teatree.loop.slack_answer`, `manage.py loop_slack_answer`, `t3 loop slack-answer {run,status,start}`) is the **third `/loop` slot**: a tight-cadence (default 20s, env `T3_SLACK_ANSWER_CADENCE`, floor 15s), **token-cheap** complement to the 720s fat tick. Where the `slack_dm_inbound` scanner (§ 5.6) only *records* user DMs as `PendingChatInjection` rows and the prompt-drain surfaces them in-band, this loop *answers* them out-of-band so a quick ack / status question gets a reply in seconds, not at the next fat tick — at near-zero token cost.

**Complementary to the drain, not a double-answer.** `consume()` stamps `consumed_at` (prompt-drain); this loop stamps `answered_at` / `eyes_reacted_at` (reply posted / receipt reacted). The three columns are orthogonal single-use CAS transitions, so a row can be drained and answered independently with no race and no double reply (§ 5.6 `PendingChatInjection`; #1014).

**Message coalescing (zero-token, pure DB/time).** Before classification, consecutive `PendingChatInjection` rows from the *same* `user_id` on the *same* `(overlay, channel)`, with no bot reply between them and received within `_COALESCE_WINDOW_SECONDS` (default 90s), are folded into ONE logical turn (`_Unit`): the text is newline-joined in received order, classified once, threaded on the FIRST row's `slack_ts`, and every row in the unit gets the :eyes: receipt and is stamped `answered_at` together. A bot reply or a `>window` gap breaks the group; a blank `user_id` (no author attribution) is never coalesced. Because the loop is the only bot that replies and stamps a whole unit at once, "no bot message between" reduces to the same-user/within-window adjacency test on the pre-reply `unanswered()` rows. This catches the "two Slack messages 3s apart that are really one request" case the user flagged, with no LLM.

**Zero-token classifier → three cheap paths.** `classify(text) -> AnswerRoute` is pure Python (no DB/network/LLM), fail-safe to `NEEDS_WORK`: `ACK_ONLY` → react ✅, `mark_answered("ack")`, no thread post; `SIMPLE` → `build_simple_answer` (Stage A is the zero-token `render_dashboard` state read; Stage B is a budget-gated `claude --model haiku` fallback), post the threaded reply, readback-verify via `get_permalink`, only THEN `mark_answered("simple")` (a post/readback failure leaves the row unanswered for retry); `NEEDS_WORK` (or Stage B sentinel / budget-closed) → create ONE PENDING `t3:answerer` Task (`Ticket(role=AUTHOR)` + `Session` + `Task(phase="answering")` — the `(AUTHOR, "answering")` pair is registered in `loop_dispatch._SUBAGENT_BY_PHASE` so the fat loop's atomic `t3 loop claim-next` spawns the bounded sub-agent; no new spawn path), post an instant ack, `mark_answered("delegated")`. The unit lead's CAS is the single idempotency boundary so a re-run never double-replies or enqueues a second answerer Task.

**Singleton + loop-owner gate.** Mirrors § 5.7: `loop_slack_answer` acquires a dedicated `LoopLease("loop-slack-answer")` (separate from `loop-tick` and `loop-self-improve`) so a long answer cycle never blocks a fast regular tick, and refuses to run when this session is not the loop owner (same `loop-registry.json` `t3-loop-tick-owner` record); manual CLI invocations outside a session bypass the gate. Per-unit `try/except` so one bad unit never blocks the rest; bounded to `_BATCH` units per cycle.

**#1107 Prong B tick piggyback.** Mirrors the § 5.7 piggyback: `tick_piggyback.run_piggyback_cycles` (won-owner success path of `loop_tick` only) also drives `run_slack_answer_cycle()` behind the **same** `LoopLease("loop-slack-answer")` CAS the dedicated slot uses. A real dedicated slot holding the lease wins the CAS so the piggyback skips (the #1014/#1075 fast path is never double-run); the per-tick-unique owner + un-released `lease_seconds=<T3_SLACK_ANSWER_CADENCE>` lease doubles as the throttle (a re-tick inside the cadence window loses the CAS → exactly one reaction per window). This is the defense-in-depth that keeps user DMs getting `:eyes:`/answered even in a pure-cron / no-session deployment where Prong A cannot resolve a loop owner.
