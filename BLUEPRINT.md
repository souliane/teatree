# TeaTree Blueprint

The product spec. Code is an artifact; this file is the product.

If the entire `src/` and `tests/` tree were deleted, this document — plus the three architectural appendices linked below and the skills in `skills/` — should be enough to regenerate the project without ambiguity.

**Tone.** This blueprint is functional and architectural. It names classes and files where the structure carries the design, but it is deliberately language-agnostic — a competent engineer should be able to reimplement teatree in another language with this file alone. Prose-of-code (paragraph descriptions of method bodies, ticket-history rationale, line-by-line walkthroughs) does not belong here. The code itself is the source of truth for implementation; this file is the source of truth for architecture.

**Change policy.** Every code change to teatree must keep this file consistent with the architecture. Implementation details — new flags, new error messages, why a particular regex was tightened — belong in docstrings, commit messages, or the issue tracker, not here. Before modifying this file (or any of the three appendices) please ask the user for approval — this is the source of truth and the user validates every change.

**Status:** current architecture under [#541](https://github.com/souliane/teatree/issues/541). All phases (0–8) shipped.

- Statusline file is the only persistent UI surface (no HTML dashboard, no daemon, no ASGI server)
- Code-host + messaging Protocols unified; backends are selectable per overlay
- Fat loop + scanners + dispatcher; `claude -p` is the headless executor and the SDK swap point
- No-overlay-leak grep gate keeps the platform tenant-agnostic

The §17.1 Invariants list is parsed by `scripts/hooks/check_blueprint_invariant_numbering.py` (gapless 1..N — do not renumber, do not delete). The §5.6 Loop Topology summary is scanned by `tests/test_blueprint_loop_epic_alignment.py`.

---

## 1. What TeaTree Is

A personal code factory for multi-repo projects. It turns a ticket URL into a merged pull request by coordinating the full lifecycle — intake, coding, testing, review, shipping, delivery — across multiple repositories, worktrees, and agent sessions.

**Target:** service-oriented projects with databases and CI pipelines (any language). Not for docs-only repos or CLI tools.

**Operating mode.** TeaTree runs as a long-lived interactive Claude Code session orchestrated by a fat `/loop` (~10–15 min cadence). The loop fans out to in-session subagents per tick to sweep PRs, auto-review PRs assigned to the user, intake assigned issues, watch messaging mentions/DMs, and render a multi-line statusline that is the **only persistent UI surface**. There is no HTML dashboard. The loop runs in the same session the user types into, so debugging stays direct.

**Code-host neutrality.** Pull requests are the canonical concept. Both **GitHub** and **GitLab** are first-class in core; GitLab MRs map onto the PR abstraction at the Protocol layer.

**Messaging-backend pluggability.** Mentions, DMs, and outgoing posts go through a `MessagingBackend` Protocol declared per overlay. Slack (Socket Mode bot) is the first implementation. A `Noop` default lets overlays opt out.

**Core principle.** Infrastructure is deterministic code; development work is skill-guided. State management, port allocation, provisioning, task routing, code-host sync, and messaging integration are Python code with >90% branch coverage. The actual development — coding with TDD, debugging, reviewing, shipping — is driven by skills that encode methodology, guardrails, and domain knowledge.

**Core stays generic.** No customer-, tenant-, or product-specific names appear in `src/teatree/` or `docs/`. Per-overlay specifics live in the overlay package and in `~/.teatree.toml`. A CI grep gate (`scripts/hooks/check_no_overlay_leak.py`) enforces this — cited as `BLUEPRINT § 1` from `tests/test_no_overlay_leak_hook.py`.

---

## 2. Architecture Principle: Code-First, Not Skills-First

Infrastructure and orchestration are code; development methodology is skill-guided prose. The split is load-bearing:

1. **Skills are prose, not code.** Prose produces different results depending on model, context, and what else is loaded. Anything that must be deterministic — state transitions, port allocation, provisioning, sync, the `/loop` tick — is code.
2. **Coordination needs transactional guarantees.** An FSM + ORM provide atomic transitions and row-locked workers. Coordination through JSON files cannot.
3. **Code is testable; prose is not.** Core logic must reach >90% branch coverage.
4. **One ABC with a handful of methods beats thirty thin extension points.** Overlay customization goes through `OverlayBase` — typed methods with defaults, no priority system, no plugin registries.

---

## 3. Package Structure

Package name: `teatree` (double-e). Repo/CLI: `teatree` / `t3`. Python: 3.13+. License: MIT. Build: `uv`. Entry point: `t3 = t3_bootstrap:main` — a top-level shim that bootstraps Django settings before importing `teatree.cli`.

Top-level layout under `src/teatree/`:

```
cli/         # Typer CLI — bootstrap commands (no Django needed)
core/        # Django app — models, FSM, managers, sync, runners, management commands
agents/      # Headless executor runtime (claude -p swap point)
loop/        # /loop topology — tick, scanners, dispatch, statusline
backends/    # Pluggable external service integrations (GitHub, GitLab, Slack, Notion, Sentry)
utils/       # Pure utilities (git, ports, db, secrets, compose contract, ...)
overlay_init/, contrib/, docker/, templates/overlay/
```

Plus top-level: `agents/` (phase sub-agent definitions), `skills/*/` (workflow skills), `hooks/` (plugin hooks), `tests/` (pytest suite), `scripts/` (utility scripts), `.claude-plugin/`, `apm.yml`, `settings.json`.

Per-overlay Slack bot setup (`t3 setup slack-bot --overlay <name>`) — and the one-command `t3 setup slack-provision` that runs the whole lifecycle (manifest scopes, OAuth URL, channel join, tokens) idempotently ([#1686](https://github.com/souliane/teatree/issues/1686)) — are detailed in [docs/blueprint/configuration.md](docs/blueprint/configuration.md) §10.1 "Slack bot setup", cited from `src/teatree/backends/slack_bot.py`, `src/teatree/cli/slack_setup.py`, and `src/teatree/cli/slack_provision.py` as `BLUEPRINT §3.6`.

---

## 4. Domain Models

Five core lifecycle models in `teatree.core.models/`, all FSM-driven (`django-fsm`): **Ticket**, **Worktree**, **Session**, **Task**, **TaskAttempt**. Around them, supporting rows under the same package: `BotPing`, `DailyDigestThread`/`DailyDigestMessage`, `DbApproval`, `DeferredQuestion`, `EvalRunRecord`/`EvalScenarioResult` (§17.6), `IncomingEvent`, `IntentClassification`, `LivePostApproval`, `LoopLease`, `MergeClear`, `OnBehalfApproval`, `OutboundClaim` (drift verifier covers `slack_dm`, `slack_reaction`, `gitlab_note`, `gitlab_approve`, `github_note` ([#1198](https://github.com/souliane/teatree/issues/1198)), `notion_comment`, `notion_edit`), `PendingChatInjection`, `PullRequest`, `RedCardSignal`, `ReplyDispatch`, `ReviewAssignment`, `ReviewRequestPost`, `ReviewVerdict`, `ScannedBroadcast`, `SelfImproveFiring`, `SessionHandover` (the §5.6 session-to-session hand-off row), `TicketTransition`, `WorktreeEnvOverride`. The supporting set is enumerative — every row name above is cited by name from §5.6, §17.1, or §17.4 prose.

`Ticket` also carries a `Role` enum (`AUTHOR` / `REVIEWER`) orthogonal to its state. The reviewer role drives the §5.6 `reviewer_prs` scanner (`Ticket(role="reviewer", issue_url=<pr_url>)`) and short-circuits to `delivered` once the review work completes; the author role is the default lifecycle.

**State machines** (one row per `Ticket` is the unit of work):

| Model | States |
|---|---|
| `Ticket` | `not_started → scoped → started → planned → coded → tested → reviewed → shipped → in_review → merged → retrospected → delivered` (plus terminal `ignored`) |
| `Worktree` | `created → provisioned → services_up → ready → torn_down` |
| `Session` | per-phase quality-gate tracker keyed `(ticket, phase, agent_id)` |
| `Task` | `pending → claimed → in_progress → completed / failed` |
| `TaskAttempt` | execution history rows (immutable) |

**§4 invariant — worker enqueue pattern (load-bearing).** Transitions that own long I/O follow one rule:

- Transition body stays pure: state change + metadata only, then `transaction.on_commit(lambda: execute_X.enqueue(self.pk))`. The state change and the queued work land atomically.
- Workers take a row lock (`select_for_update()`), re-check the source state, run the runner, and on success call the next transition.
- At-least-once delivery is safe because the state guard makes redelivery a no-op.
- `post_transition` signals are reserved for lossy cross-cutting side effects (audit log, Slack reactions) — never for the main work of the transition.

Auto-scheduling chains the phases: `start → provision worker → schedule_planning`, `plan → schedule_coding` (gated on `PlanArtifact` DB record — no artifact raises `NoPlanArtifactError`; the only path from STARTED to CODED passes through PLANNED), `code → schedule_testing`, `test → schedule_review`, `review → schedule_shipping` (gated on `has_shippable_diff()`). The runner classes live in `core/runners/`; the workers in `core/tasks.py` and `core/worktree_tasks.py`.

**Who drains the queue.** No always-on `db_worker` is assumed; a won-owner tick drains a bounded batch of `DBTaskResult` jobs in-process (`teatree.loop.queue_drain`), standing down while a real `db_worker` holds the `teatree-worker` singleton, and first expires READY jobs older than `T3_QUEUE_STALE_HOURS` (default 24h) to the reversible `FAILED` state so a fresh drainer never fires stale heavy jobs.

**Clean-worktree preflight (#884).** `code()`, `test()`, `review()`, `ship()` refuse the transition if any worktree has tracked uncommitted changes. The refusal raises a `DirtyWorktreeError` (an `InvalidTransitionError` subclass, **not** `TransitionNotAllowed`) inside the caller's outer atomic block, so the FSM state change is rolled back and the lease reaper returns the CLAIMED task to PENDING on the next tick. No auto-stash — worktrees share one `.git`, so a stash is repo-global and could clobber an unrelated branch's work.

**DoD local-E2E preflight ([#88](https://github.com/souliane/teatree/issues/88)).** `ship()` runs a second preflight (`core/dod_gate.check_local_e2e_dod`) after the clean-worktree one: a UI-visible ticket (scoped `repos` intersect the overlay's `frontend_repos`) needs a green local-stack E2E artifact before leaving the implementation lifecycle. The artifact is `Ticket.extra['e2e_recipe'].last_run` with `result == "green"` AND `env == "local"` (`record_run` stamps `env`, default `local`); a `dev` run does not satisfy. Refusal raises `DodLocalE2EError` (an `InvalidTransitionError` subclass). Hatch: `Ticket.extra['dod_e2e_override']` with a non-empty `reason` (`ticket dod-override <id> --reason …`) passes-and-logs; an overlay with no `frontend_repos` is silent. **Fail-closed UI-visibility ([#1426](https://github.com/souliane/teatree/issues/1426)).** When the overlay cannot be resolved, `is_ui_visible` fails closed (presumes UI-visible, logs loudly) so a misconfigured instance cannot silently skip the gate; the hatches prevent a hard lockout.

**Mandatory-E2E gate for customer-display-impacting changes ([#1967](https://github.com/souliane/teatree/issues/1967)).** `core/e2e_mandatory_gate` runs at the `pr create` ship-gate and §17.4 `ticket clear`: a change the fail-closed classifier `OverlayBase.classify_customer_display_impact` (pure `core/customer_display_impact`; dogfood `False`, product overlay declares non-impacting paths) marks impacting needs a satisfier (like `MergeClear`): a green AND **posted** SHA-bound `E2eMandatoryRun` (the `e2e post-evidence` comment `posted_url` via `lifecycle record-e2e-run`; recorded-but-unposted does not satisfy), a single-use **user** `E2EBypassApproval` (`ticket e2e-bypass`; maker/agent/loop refused — maker≠checker, audited), or the `[teatree] e2e_mandatory_gate_enabled` kill-switch. Deny names both remedies — satisfiable, never a lockout. Additive to #88 (which keys on frontend repos; this keys on diff content, user-approved bypass).

**Fix-ticket FixRecord DoD gate ([#1661](https://github.com/souliane/teatree/issues/1661)).** `Ticket.kind` (`feature` default, `fix`). For `kind=fix`, `mark_delivered` (RETROSPECTED → DELIVERED) runs `core/fix_dod_gate.check_fix_record_dod`: refused unless `Ticket.extra['fix_record']` has every field (`root_cause`, `evidence`, `regression_test`, `observed_red`, `recurrence_fingerprint`) non-empty (done = root cause + verified, not a merged manifestation patch). Refusal raises `FixRecordDodError` (an `InvalidTransitionError` subclass) so the ticket stays RETROSPECTED — merged, not *done*. `Ticket.extra['fix_record_override']` `reason` passes-and-logs; `feature` tickets pass. Mirrors `check_local_e2e_dod`.

**Reviewing-phase review-skill evidence gate ([#1539](https://github.com/souliane/teatree/issues/1539)).** Recording the `reviewing` attestation (`lifecycle visit-phase <id> reviewing`) runs `core/review_skill_gate.check_review_skill_evidence` after the reviewer-identity check. When `review_skill` (env `T3_REVIEW_SKILL`, per-overlay/global overridable) is set, the visit is refused (`ReviewSkillEvidenceError`) unless `Ticket.extra['review_skill_run']` attests the configured skill ran (`lifecycle record-review-skill-run <id> <skill>`). Empty `review_skill` (default) is a NO-OP. Distinct from `architectural_review_skill` (the periodic cadence scanner).

**Review deep-retrieval constraint — path-independent.** Reviewing carries the same responsibility as implementing: a verdict from the diff alone is not a review. `Ticket.review_context_satisfied()` is a `django_fsm` condition on the substantive-verdict transitions `review()` and `mark_reviewed_externally()`, so when `require_review_context` (per-overlay/global, default `false`) is on, every verdict path is mechanically refused (`TransitionNotAllowed`) until `core/review_context_gate.is_complete` accepts `Ticket.extra['review_context']` — the work item fetched from its source, links followed, and referenced documents downloaded + analyzed against the diff. One condition covers all entry points: the FSM `reviewing` phase (`visit-phase` raises `ReviewContextError`), the workflow path (`Task.complete()` → the transition), and the direct `t3 ticket transition <id> review` driver. Stamped via `lifecycle record-review-context`; a partial record never satisfies it. Default-off is a NO-OP. `mark_review_no_action` (bot/no-diff disposition) is exempt.

**Anti-vacuity attestation gate ([#1829](https://github.com/souliane/teatree/issues/1829)).** `core/anti_vacuity_gate` extends the §17.4 CLEAR machinery so a maker cannot request-review/merge an MR whose new regression test is *vacuous* (green even with the bug present). A pure function over `Ticket.extra['anti_vacuity_attestation']` + the live head SHA (mirroring `review_skill_gate`/`review_context_gate`); opt-in via `require_anti_vacuity_attestation` (default `false` NO-OP). On: **merge** (`assert_merge_preconditions` after the §17.4.3 SHA-match) and **request review** (`review_request_post`, keyed on `--ticket-id`+`--head-sha`) refuse unless the attestation maps the diff to the acceptance criteria and proves each new test anti-vacuous (revert fix → RED) or claims `no_new_tests`. SHA-bound like `MergeClear.reviewed_sha` (stale head → re-attest). Stamped via `lifecycle record-anti-vacuity`. A safety floor — `autonomy = full` does NOT collapse it.

**Sync writers honor the same gate ([#1426](https://github.com/souliane/teatree/issues/1426)).** The DoD decision is factored into `dod_gate.sync_gate_allows` so every code-host sync writer — not just `ship()` — respects it, closing the bypass where sync wrote a post-ship state DIRECTLY (outside the FSM). A state is **post-ship** when its lifecycle index is at or past `SHIPPED` (so `SHIPPED`, the higher `IN_REVIEW`, and terminal `MERGED`/`DELIVERED`). Two flavors: (1) a **workflow** state inferred from a live open PR (`gitlab_sync_prs` infers `SHIPPED`/`IN_REVIEW`) is *capped* — `workflow_capped_state` demotes a gate-refused state to `STARTED` and leaves the ship transition to own it; (2) a **terminal** state reflecting an external fact (a genuinely merged PR → `MERGED` in `gitlab_sync_terminal`, a board "Done" → `DELIVERED` in `github_sync`) is **never demoted** — that would make the ticket contradict reality, mirroring how the `reconcile_merged` FSM keystone follows an authorised post-hoc merge. Instead `record_terminal_dod_violation` keeps the terminal state but records a durable `Ticket.extra['dod_e2e_violation']` audit marker and logs loudly when the DoD was unmet, so the gap is auditable rather than silent. Pre-ship inferred states (`NOT_STARTED`/`STARTED`) pass through unchanged.

**Concurrent-stack cap (#1397).** `max_concurrent_local_stacks` (in `[teatree]` / `[overlays.<name>]`) caps the number of distinct tickets whose worktrees can be in `services_up`/`ready` at once for a given overlay. The gate (`teatree.core.local_stack_gate.check_local_stack_limit`) runs before `Worktree.start_services()` in `t3 <overlay> worktree start` and `workspace start`; on breach it raises `LocalStackLimitExceededError` naming the blockers. Default `0` keeps the legacy unbounded behaviour; set `1` on a heavy overlay to prevent local-stack OOMs. Per-overlay overridable; sibling worktrees of the same ticket count as one logical stack.

---

## 5. Agent Execution

The agent layer is `teatree.agents` (headless executor + prompt + skill bundle + structured result schema) and `teatree.loop` (the /loop topology). The orchestrator-as-keystone contract is §17.8 — every implementation, review, test, debug, and ship action is dispatched to a sub-agent; the orchestrator's job is synthesis and dispatch, not execution.

**Headless executor (`agents/headless.py`).** Runs `claude -p <prompt> --append-system-prompt <context> --output-format json`. Kept deliberately slim — the swap point for an Anthropic SDK runtime. A heartbeat-driven `LoopWatchdog` bounds runaway subprocesses; a per-ticket `TicketBudget` caps cumulative cost.

**Interactive-by-default phase dispatch (post-2026-06-15 billing).** A detached `claude -p` is metered now, so loop-dispatched phase work runs as in-session `Agent` sub-agents (subscription). The `Task.save` invariant routes a task whose `(role, phase)` has a registered agent (`Task.loop_dispatched`) to `INTERACTIVE`; free-form phases stay HEADLESS. The `/loop` slot claims each (`claim-next --json`, carrying loop-resolved `model`+`skill_bundle`), spawns its `Agent`, then records the result with `tasks record-attempt` — shared `agents/attempt_recorder` applies the same schema+evidence gate (#1284) as `claude -p`. Fail-closed: `execute_headless_task` refuses a loop phase unless `LOOP_ALLOW_HEADLESS_DISPATCH` (default `False`). Evals follow the same split: `t3 eval run` **defaults to `--backend subscription`** (grade in-session transcripts, no API spend); CI passes `--backend sdk` to keep the budgeted `claude -p`/SDK path (`ANTHROPIC_API_KEY`). `--trials`/`--models` always force the metered SDK runner. The subscription transcript is the sub-agent JSONL (`t3 eval capture-subagent` copies it to the grader path); `SubscriptionTranscriptRunner` auto-detects its session schema (no `result` event; terminus via final assistant `stop_reason`) and grades on matchers off the file. **All-skipped guard ([#1811](https://github.com/souliane/teatree/issues/1811)).** A run that never happened (`claude` not on PATH / no key) SKIPs as passed, so an execute-none suite exits green with no coverage. `t3 eval run --require-executed` exits non-zero when `collected > 0` but `executed == 0`; both CI mirrors arm it only when `ANTHROPIC_API_KEY` is set, so the no-key case stays a visible skip and fires once a key exists. The flag is opt-in so the subscription backend's pre-transcript all-skip stays green. The SDK `claude -p` runner and judge both run **virgin** (`eval/isolation.isolated_claude_env` + `--bare`): the child's `HOME`/config-dir vars point at an empty temp dir and the cwd is neutral, so the developer's `~/.claude/CLAUDE.md`, auto-memory, and project `CLAUDE.md` never bias a result (credential/`PATH` survive).

**Structured result schema (`agents/result_schema.py`).** Agents return JSON: `summary`, `files_modified`, `tests_run`, `tests_passed`, `tests_failed`, `decisions`, `needs_user_input`, `user_input_reason`, `next_steps`, `commands_executed`. `additionalProperties: false`. Validated without the `jsonschema` library to keep the dep tree small.

**Skill bundle (`agents/skill_bundle.py`) + delegation map (`skill_map.py`).** A phase → companion-skills map (e.g. `coding → test-driven-development`), topo-sorted `requires:` resolution, per-overlay `companion_skills` (#1132). Reviewer-dispatch review skills come from `OverlayConfig.get_review_companion_skills()` (deduped `[pr_review_companion, *companion_skills]`, #1135 default `code-review`); on the `reviewing` phase `build_system_context` embeds them IN FULL, else a `claude -p` reviewer (which never auto-loads skills) sees only the demoted summary and reviews without overlay knowledge. For an *orchestrator-built* reviewer dispatch (Agent tool / dynamic workflow), `agents/prompt.build_reviewer_dispatch_prompt()` is the single shared builder: it prepends a REQUIRED Skill-tool load block (lifecycle `t3:review` + the overlay review skills) to the review instruction, so the conventions reach the reviewer structurally rather than via orchestrator memory; the `shipping`-phase auto-review gate emits this block verbatim ([#1368](https://github.com/souliane/teatree/issues/1368)). The **coding phase** force-loads a symmetric contract: `build_task_prompt`/`build_system_context` emit a REQUIRED load block (`/t3:architecture-design`+`/t3:code`), embed `architecture-design` in full (`_CODING_PHASE_ALWAYS_FULL`), and carry behavior-preservation (a rewrite enumerate-and-preserves old behavior, never silently narrows a gate or inverts a must-block test) + no-AI-signature clauses, so the enumerate-preserve discipline reaches a skill-stripped builder instead of being a post-hoc cold-review catch.

**Model tiering.** `agents/model_tiering.resolve_phase_model(phase)` downgrades mechanical phases (`reviewing`/`testing`/`shipping` → sonnet, `retrospecting` → haiku) by default; reasoning phases (`coding`, `debugging`) inherit the user's default. Per-phase overrides via `[agent] phase_models` in `~/.teatree.toml`.

### 5.6 Loop Topology

TeaTree drives the day from a single long-lived Claude Code session running a fat `/loop`. The loop fires on a fixed cadence (default 12 minutes via `[teatree] loop_cadence_seconds`). The tick body is `teatree.loop.tick.run_tick` — code, not prose, so it is tested, typed, and version-controlled. `run_tick` composes named single-responsibility phases from `teatree.loop.phases` ([#1796](https://github.com/souliane/teatree/issues/1796)): `scan_phase` (parallel world-scan), `sweep_phase` (split the maintenance scanners — `pr_sweep`/`self_update`/`pull_main_clone` — out of the scan), `act_phase` (dispatch + mechanical + persist), and `orchestrate_phase` (the speed-driven autonomous fan-out — a no-op at the default `medium` speed; `slow` admits at most one, `full`/`boost` clamp a claimed manifest to the per-overlay `max_concurrent_auto_starts` budget via the existing claim-next CAS, computing + returning the manifest only — spawning stays in the session/self-pump half).

**#786 epic — the immortal-singleton roster model is fully retired (WS1–WS5 + #54, all merged).** The original loop model — a coordinator spawning a fixed roster of long-lived loop sub-agents it had to keep alive and re-spawn on death/compaction — was the root cause of the recurring "loop died on compaction / had to be re-spawned" toil and the duplicate-on-restart hazard. It is **fully retired**: no roster, no `spawn_brief`, no takeover-respawn, no resume-by-agentId. The replacement satisfies three acceptance-contract invariants, each delivered by a specific workstream and detailed in the appendix:

- **Invariant 1 — 0 sessions ⇒ nothing runs.** The loop is session-bound; zero open sessions ⇒ the loop is dormant, by design (WS3). The optional macOS LaunchAgent installed by `t3 loop install-watchdog` ([#1139](https://github.com/souliane/teatree/issues/1139)) is a session-watchdog, not an OS daemon: it re-runs `t3 loop spawn-headless` on Claude Code exit and after `/login` account switches so a session is normally available; the loop itself still runs only inside an open session.
- **Invariant 2 — ≥1 session ⇒ exactly one machine-wide tick.** Driven by the recurring `t3 loop tick` cron; the executor mutex is the WS2 `LoopLease` DB row (backend-agnostic conditional-UPDATE CAS, expiry-reapable — #54 removed the dead renew/heartbeat), and the WS3 single Django-free `_OWNER_LOOP` tick-owner record names which session ticks. Atomic per-unit claim is WS1 `t3 loop claim-next` (claim == spawn boundary; no double-dispatch). A second concurrent tick loses the CAS and SKIPs.
- **Invariant 3 — exactly one TODO-consolidation loop per agent identity, across all sessions.** The WS4 per-agent consolidation self-pump, keyed by `agent_id` in a separate consolidation-registry.

**Subsumed issues (WS5 — documented, not closed here).** [#789](https://github.com/souliane/teatree/issues/789) (a non-owner session still arming the tick cron) is **subsumed**: under the WS1 claim/lease a non-owner tick simply finds nothing to claim, so the concern dissolves rather than needing a separate fix — #789 was closed-as-completed when WS3 landed and is **not** reopened. Board task #50 (the per-agent TODO-consolidation loop) is **subsumed by invariant 3 / WS4**; #50 is a project-board card, **not** a repository issue, so it is documented as subsumed here and tracked on the board — there is no repo issue to close. WS5 itself carries no GitHub closing keyword on the #786 umbrella; only an explicitly-authorized epic-completion step does.

**Deep mechanics live in [docs/blueprint/loop-topology.md](docs/blueprint/loop-topology.md).** The DB-lease singleton, the session-scoped loop-owner claim and `SessionStart` tick-owner record, the per-agent self-pump, the Stop-gate family, post-compaction snapshot recovery, the three-stage tick (scan → dispatch → render), the full scanner set, the multi-overlay / multi-host / multi-identity scanning, the auto-start / dispositions / completion phases, and §5.6.1 Statusline rendering / §5.6.2 Mode + training-wheel / §5.6.3 Availability all live there. Top-level architectural notes that are teatree-CORE always-on or load-bearing for cross-references: the periodic `architectural_review` cadence-and-merge-count scanner (always-on for every overlay, `architectural_review_disabled` escape hatch); the daily `scanning_news` scanner ([#1191](https://github.com/souliane/teatree/issues/1191)) gated by `scanning_news_disabled`, with the [#1391](https://github.com/souliane/teatree/issues/1391) ask-gate (`ask_before_creating_news_tickets`, default on) recording each candidate as a `PendingArticleSuggestion` rather than auto-filing; the daily `dogfood_smoke` and `eval_local` scanners ([#1308](https://github.com/souliane/teatree/issues/1308), `dogfood_smoke_disabled`); the always-on `review_request_merge_react` scanner ([#1797](https://github.com/souliane/teatree/issues/1797)) that reacts `:merge:` on a review-request's Slack message once its MR merges (#1750 `react_routed` routing); the closure-reverify Stop WARN ([#1448](https://github.com/souliane/teatree/issues/1448), `teatree.hooks.closure_reverify_scanner`, non-blocking so it cannot deadlock the loop); the `SessionHandover` hand-off (`t3 <overlay> handover`, claimed+injected on `SessionStart`); and the public `jobs_for_domain(domain, backend, *, all_backends)` seam ([#1482](https://github.com/souliane/teatree/issues/1482), `Domain` StrEnum) that partitions the per-overlay scanner fan-out into one typed surface.

The [#1554](https://github.com/souliane/teatree/issues/1554) `issue_implementer` mini-loop closes the auto-implement-intake gate: each tick its per-overlay scanner lists the overlay's open issues, keeps the ones carrying `issue_implementer_label`, and claims each via the TOCTOU-safe `ImplementedIssueMarker.claim` so two concurrent ticks never double-dispatch. It is **default-OFF behind a triple gate** — the master `issue_implementer_enabled` flag (default `false`), the `ImplementedIssueMarker.in_flight_count(overlay) < issue_implementer_max_concurrent` budget (default 1), and the per-issue `claim()` idempotency — gated by `[teatree]` config (`issue_implementer_enabled` / `issue_implementer_label` / `issue_implementer_max_concurrent` / `issue_implementer_cadence_hours`, per-overlay overridable, with the `T3_ISSUE_IMPLEMENTER_ENABLED` env kill-switch). Enabled with an empty `issue_implementer_label` is a safe no-op that logs one WARNING so the operator sees why nothing dispatches. Each newly-claimed issue emits `issue_implementer.claimed`, which routes to `t3:orchestrator` as a **maker-side kickoff** — it starts the normal maker pipeline for the issue, issues no `MergeClear`, and gains no merge authority (the §17.4 maker≠checker boundary is untouched). The scanner **skips any issue carrying `NEEDS_TRIAGE_LABEL`** (`needs-triage`) before the claim — a maintainer-applied hold so the factory never starts an issue the maintainer has not cleared. Since the factory files issues *as* the maintainer, the author-only auto-apply Action can't gate agent-filed issues; agents self-apply `needs-triage` on anything they file that is not a direct user order (`FilingContext.auto_filed`).

### 5.7 Self-Improving Monitor

A detector swarm that rides the same tick the regular `/loop` runs. It watches for smells the rest of the loop cannot self-report — dispatcher silently skipping a phase, a `MergeClear` issued but never reconciled, a statusline entry whose evidence has gone stale — and converts each into a `SelfImproveFiring` row plus a graduated action (`log → statusline → slack → ticket → auto_fix`, monotonic ladder). It is the legibility substrate §§17.4–17.8 relies on. Auto-fix is whitelisted: today only `StaleStatuslineEntryDetector` carries `auto_fix = True`. The shipped detector set (`detectors/registry.py`) is `DispatchGapDetector`, `ForgottenMergeDetector`, `StaleStatuslineEntryDetector`; additional `auto_fix` slots land with their own structural whitelist test.

Sibling loop scanners (under `loop/scanners/`, not `SelfImprove` detectors) close the gaps the detectors only surface:

| Scanner | Closes | Contract |
|---|---|---|
| `PrSweepScanner` ([#1248](https://github.com/souliane/teatree/issues/1248), wired [#1257](https://github.com/souliane/teatree/issues/1257)) | forgotten / conflicted / un-reviewed merges | Invokes the §17.4 keystone merge for any open PR whose `MergeClear` is actionable, head SHA matches, and required checks are green (`--fallback-uv-audit` escalation when the only red check is `uv-audit` and `main` is red too). A conflicted PR emits `pr_sweep.flag_conflict` ([#78](https://github.com/souliane/teatree/issues/78)), flag only — never an auto-rebase; a solo bypass lacking a recorded cold-review emits `pr_sweep.flag_no_review` ([#68](https://github.com/souliane/teatree/issues/68)) (below). |
| `SlackBroadcastsScanner` ([#1131](https://github.com/souliane/teatree/issues/1131), wired [#1255](https://github.com/souliane/teatree/issues/1255)) | inbound review-request | Polls the review channel for MR-link broadcasts → `slack.review_intent` dispatch without an explicit reaction. |
| `SelfUpdateScanner` ([#1249](https://github.com/souliane/teatree/issues/1249)) | editable-install drift | Ff-only updates the editable teatree clone (`T3_REPO`) + every overlay clone on `self_update_cadence_hours` (1h); `SelfUpdateMarker` carries the cadence. |
| `PullMainCloneScanner` | stale work-repo main clones | Same ff-only contract for the `$T3_WORKSPACE_DIR` main clones a worktree is created from, so a clone parked behind never poisons `git show` / `grep`; `pull_main_clone_cadence_hours` (1h), `PullMainCloneMarker`. |
| `CodexReviewScanner` ([#1254](https://github.com/souliane/teatree/issues/1254)) | self-review vigilance gap | Auto-dispatches `/codex:review` on every self-authored PR push (keyed `(slug, pr_id, head_sha)` via `CodexReviewMarker`; `codex:adversarial-review` variant for `auth/`/`permissions/`/`migrations/`/secret paths). |
| `ResourcePressureScanner` ([#128](https://github.com/souliane/teatree/issues/128)) | host OOM / full disk | Measures **absolute** free disk + reclaimable RAM (never percent-of-nominal, so APFS/macOS reporting cannot mis-fire); L0 OBSERVE / L1 WARN / L2 CRITICAL (`free_resources` allow-list cache purge + idle-docker stop) / L3 DESTRUCTIVE (flag-gated worktree GC + renderer SIGTERM). Every destructive lever defaults OFF; dry-run-first, best-effort, `resource_pressure_disabled` kill-switch; `ResourcePressureMarker`. |
| `TodoSweepScanner` ([#129](https://github.com/souliane/teatree/issues/129)) | stale TODOs | Verifies each open `Task`'s artifact via `is_issue_done`; terminal → `todo.completion_detected` re-checks live (fail-CLOSED) before `Task.complete`; unverifiable → `todo.orphaned` (fail-OPEN). Per-item, idempotent via `Task.last_sweep_check_ts`; `todo_sweep_disabled`. |

`CodexReviewScanner`'s and `PrSweepScanner`'s auto-dispatch is gated on the fleet doctrine (`mode = "auto"` + `require_human_approval_to_merge = false`); every other overlay stays manual. All scanner knobs are per-overlay overridable.

The monitor never auto-merges substrate, never auto-edits memory / skills / `BLUEPRINT.md`, and never bypasses the §17.4 `MergeClear` reviewer-attestation requirement **except** on a solo overlay the user has explicitly declared end-to-end-trusted (`mode = "auto"` + `require_human_approval_to_merge = false`) — where the per-diff CLEAR cannot be issued because maker and reviewer are the same human identity and `MergeClear.issue` mechanically refuses a self-attested CLEAR (`is_non_reviewer_role`), so an unrelaxed gate would silently no-op every green PR forever ([#1309](https://github.com/souliane/teatree/issues/1309)). The carve-out is *minimal*: every precondition gate (draft, changes-requested, CI verdict, uv-audit escalation) stays in force; the per-diff CLEAR row is replaced by the `gh pr merge --squash` fallback, but the **cold-review floor still holds** — the bypass requires a recorded INDEPENDENT `merge_safe` `ReviewVerdict` at the live head (`reviewer != maker`), else it does NOT merge ([#68](https://github.com/souliane/teatree/issues/68)), so the only-identity-on-repo maker can never self-merge. Overlays that did not opt in keep the CLEAR requirement in full force.

**Auto-review dispatch closes the no-review loop ([#68](https://github.com/souliane/teatree/issues/68)).** The cold-review floor above leaves a gap: on a full-autonomy solo overlay a green own PR with no recorded verdict only logged `flag_no_review` and sat open until a human noticed — nothing dispatched the cold review the floor demands. The scanner now closes it: on `flag_no_review` for a green+clean+non-draft own PR (gated on `auto_review_dispatch = solo_overlay and not require_human_approval_to_merge`), it enqueues ONE claimable `Task(phase=reviewing)` via `AutoReviewDispatch.enqueue`, deduped per `(slug, pr_id, head_sha)`: a new push re-arms exactly one task, an open task for the same head never duplicates, and a recorded verdict for the head suppresses enqueue upstream (the PR merges instead of reaching `flag_no_review`). The task's `execution_reason` carries the reviewer contract — cold-review per `/t3:review` then `t3 <overlay> review record <pr_id> <slug> --reviewed-sha <SHA> --reviewer-identity <id> --verdict merge_safe`. The loop self-pump dispatches the pending task to `t3:reviewer`; its recorded `merge_safe` verdict is the artifact the NEXT sweep merges on. A human-approval-required overlay never reaches `_evaluate_solo_overlay` (it runs the CLEAR path), so it is never armed.

### 5.8 Reactive Slack-Answer Loop

A tight-cadence (default 20s), token-cheap third `/loop` slot that answers user DMs out-of-band so a quick ack / status question gets a reply in seconds, not at the next fat tick. Coalesces consecutive same-user messages into one logical turn, classifies (pure Python) into `ACK_ONLY` / `SIMPLE` / `NEEDS_WORK`, and either reacts, posts a threaded reply, or delegates to the `t3:answerer` sub-agent.

---

## 6. Overlay System

An overlay is a downstream Django project that customizes teatree for a specific project/organization.

**§6.0 Overlay Thinness Principle (Non-Negotiable).** Generic workflow logic belongs in core, not in overlays. Before adding logic to an overlay, ask: "Would a different project using the same framework need the same logic?" If yes, it belongs in core — parameterized and configurable. Overlays should provide only: (1) configuration values, (2) project-specific glue, (3) truly unique workflows. Everything else — DB provisioning strategies, migration runners, symlink management, service orchestration — is a configurable engine in core. The overlay configures the engine; the overlay does not reimplement the engine. If an overlay method exceeds ~30 lines of non-configuration code, it likely contains generic logic that should be extracted.

**OverlayBase ABC (`teatree.core.overlay`).** Composition over inheritance: `overlay.config` is an `OverlayConfig` dataclass; `overlay.metadata` an `OverlayMetadata`. All methods take a `worktree` for context.

| Required | Returns | Purpose |
|---|---|---|
| `get_repos()` | `list[str]` | Repositories to provision |
| `get_provision_steps(worktree)` | `list[ProvisionStep]` | Ordered setup steps |

Optional hooks cover env, services, Docker base images, DB import strategy, post-DB steps, symlinks, PR validation, sync targets, skill metadata, CI config, E2E config, variant detection, tool subcommands, visual QA targets, E2E env extras + preflight, and the auto-merge guard (`can_auto_merge` → `MergeGuard`). All default to empty / permissive so existing overlays keep working — core enforcement only activates for overlays that opt in.

**Overlay API version pin.** `teatree.__overlay_api_version__` is bumped on any breaking change to the overlay-facing API. Overlays assert this at import to fail loudly when teatree diverges from what they were built against.

**Docker base-image sharing (§6.2a).** Teatree builds each `BaseImageConfig` exactly once per `(image_name, lockfile-hash)` and shares the image across every worktree that needs it. Code isolation is volume-mount level; the image itself is shared.

**`t3 startoverlay <name> <dest>`** scaffolds a lightweight overlay package: `src/<name>/{__init__,overlay,apps}.py`, `skills/overlay/SKILL.md`, `pyproject.toml`. No `manage.py`/`settings.py`/`urls.py` — teatree is the Django project.

**Discovery:** `~/.teatree.toml [overlays.<name>]` wins over `teatree.overlays` entry points on name conflicts. `discover_active_overlay()` returns the unique overlay if one exists, else the one whose `manage.py` is in cwd ancestors.

---

## 7. Backend Protocols and ABCs

Every external API concern is a `@runtime_checkable Protocol` in `teatree.backends.protocols`. PR is the canonical term in core; GitLab MRs are translated at the API edge.

| Protocol | Implementations |
|---|---|
| `CodeHostBackend` — PR/issue/comment (incl. `list`/`update_issue_comment`)/upload/review-state | `GitHubCodeHost`, `GitLabCodeHost` |
| `CIService` — pipeline cancel/trigger/quality-check | `GitLabCIService` |
| `MessagingBackend` — mentions/DMs/post/reply/react | `SlackBotBackend`, `NoopMessagingBackend` |

Request parameters are grouped into frozen `slots=True` dataclasses (`PullRequestSpec`, `MessageSpec`). `repo + pr_iid` is the natural unit on both code hosts — protocol methods never accept free-form PR URLs.

**Selection.** Per-overlay configuration (`~/.teatree.toml`) declares `code_host = "github" | "gitlab"` and `messaging_backend = "slack" | "noop"`. The loader (`backends/loader.py`) resolves the overlay's selected backend with no platform branches in caller code, cached `lru_cache(maxsize=1)` per overlay identity.

**Inbound events.** `t3 slack listen` runs a Socket Mode receiver that writes events to append-only JSONL queues (`slack-events.jsonl`, `slack-reactions.jsonl`) so scanners can drain atomically without racing.

**Reaction surface (#1281).** FSM `reactions.add` failures (`missing_scope`, `not_in_channel`, `mcp_externally_shared_channel_restricted`, …) raise `SlackReactionError` from `backends/slack_react_errors.py` — never silently return `False` — so callers cannot fall back to a `chat.postMessage(text=":emoji:")` thread reply. `SlackBotBackend.post_message` / `post_reply` reject bodies matching `^:[a-z0-9_+\-]+:$` with `SingleEmojiBodyRefusedError`, foreclosing the failure-mode shape at the backend boundary. FSM-side wrappers (`add_reactions_for_transition`, `add_approval_reaction`) catch the raise locally so Slack auth gaps cannot roll back FSM transitions.

**Destination-routed post/react ([#1750](https://github.com/souliane/teatree/issues/1750)).** Token picked by *destination* via `SlackBotBackend._route_token`: user's own DM → bot; colleague/channel → personal `xoxp`. Every colleague-surface post/react under the user's identity routes through the one gate+route+audit chokepoint `core/on_behalf_egress.OnBehalfSlackEgress` (self-DM acks ungated); the call-site authorization is pinned by the `on-behalf-routed-egress` / `on-behalf-colleague-primitives` entries in the chokepoint registry (`quality/chokepoints.yaml`, enforced by `scripts/hooks/check_chokepoints.py`); see [`skills/rules/SKILL.md`](skills/rules/SKILL.md).

**Deterministic reference linkifier.** The clickable-references rule (a bare `#N` / `!N` on a user-facing surface must become a clickable link) is enforced *in code* by `core/reference_linkifier.py`, not by asking the model. `ReferenceResolver` resolves DB-first (`PullRequest`, `Ticket.issue_url`) then constructs from the active repo context (overlay `code_host` + git-remote slug); the overlay hooks `resolve_mr_token` / `resolve_issue_token` default to it. `linkify` (idempotent; skips linked refs, inline code, fenced blocks; leaves unresolvable refs alone) runs at the Slack chokepoint (`SlackReplier._deliver`). Forge posts are NOT linkified (forge auto-links bare ids). It is the sole mechanism for the rule — the former PreToolUse/Stop bare-reference *blocking* gate was removed (it over-blocked routine commands and asked the model to rewrite refs non-deterministically); the linkifier rewrites in code with no block and no model round-trip.

**Sync ABC (`core/sync.py`).** `SyncBackend` is an ABC with `is_configured(overlay)` and `sync(overlay) → SyncResult`. Implementations: `GitHubSyncBackend`, `GitLabSyncBackend`. Both consume `CodeHostBackend` — platform-specific code lives only in the Protocol implementation, not in sync logic.

---

## 8. Command Tiers

| Tier | Tool | Needs Django? | Examples |
|---|---|---|---|
| Runtime | django-typer management commands | Yes | `worktree provision`, `tasks work-next-sdk`, `followup sync` |
| Bootstrap | Typer CLI (`t3`) | No | `t3 startoverlay`, `t3 info`, `t3 ci cancel` |
| Overlay | Typer CLI delegating to `manage.py` via subprocess | Indirectly | `t3 <overlay> start-ticket`, `t3 <overlay> worktree start` |

Internal utilities (`utils/`) are Python modules, not a CLI tier.

**Runtime commands** (`core/management/commands/`): `lifecycle`, `tasks`, `followup`, `workspace`, `worktree`, `db`, `env`, `run`, `pr`, `ticket`, `tool`, `e2e`, `overlay`, `standup`, `checking`, `availability`, `retro`, `loop_tick`, `generate_*_docs`. Each is a django-typer command group with subcommands. `db query` and `db shell` enforce read-only at two layers (leading-keyword filter + transaction `READ ONLY` / `query_only=ON`).

**Retro enforcement tooling** (`t3 <overlay> retro review-findings`, [#1573](https://github.com/souliane/teatree/issues/1573)): the scaffold behind invariant 6 / §17.6. Fingerprints a PR's review comments, records the **supplied** A/B/C verdict (never auto-guessed), and files one deduped enforcement issue per class-C finding (`create_issue`/`search_open_issues`, fingerprint marker → never refiles). The untrusted comment text is the leak vector, so its bare refs are neutralized and the rendered body banned-term scanned before filing (a hit withholds the issue) — the `gh api` stdin path bypasses the PreToolUse publish gate, so this is the only guard.

**Checking report** (`t3 <overlay> checking show`, #1529): a terse, read-only "what did I miss" catch-up for when the user checks in mid-loop. By default aggregates ALL configured overlays into one `AllOverlaysReport`; `--this-overlay` restores single-overlay scope. Each overlay has its own `checking_checkpoint_<overlay>.json` marker (atomic `tmp.replace` write, tolerant read); each marker advances independently AFTER gathering so a second run sees an empty window. Three groups: Merged / In-flight / Needs you, every reference clickable, capped at 5. `DeferredQuestion` is queried ONCE for the whole report; overlay-scoped items carry an `[overlay]` inline tag. A window start at/after `now` falls back to the 24h lookback; advance is monotonic. `--since` and `--no-advance` inspect without advancing. The needs-you group is overlay-extensible via `OverlayBase.get_checking_sources()` (default `[]`); core makes no live forge calls.

**Global CLI** (`cli/`): `t3 startoverlay`, `t3 agent`, `t3 info`, `t3 sessions`, `t3 cost`, `t3 docs`, `t3 ci ...`, `t3 review ...`, `t3 review-request ...`, `t3 tool ...`, `t3 config ...`, `t3 doctor ...`, `t3 update`, `t3 setup ...`, `t3 assess`, `t3 infra ...`, `t3 loop {start,stop,status,tick,slack-answer,claim-next}`, `t3 recover`, `t3 overlay {install,uninstall,status,contract-check}`. `t3 cost` reports cycle-to-date SDK-equivalent spend of headless `claude -p` usage vs the monthly Agent-SDK credit from each `TaskAttempt`'s captured cost/tokens/model. The dev-loop install commands (`t3 overlay install <name>`) editable-install a sibling overlay checkout into a teatree feature worktree — refuses to run in the main clone.

**Attachment ingestion** (`t3 tool to-markdown <file>`, #1479): converts binary spec attachments (PDF, XLSX, DOCX, PPTX) to Markdown so the agent can read them as structured text. Wraps `markitdown` (`teatree.backends.markdown_conversion.MarkdownConverter`) behind the **optional** `markdown` extra (`markitdown[pdf,docx,xlsx,pptx]` — never `[all]`); absent the extra the command exits non-zero with an install hint rather than crashing. Plugins are disabled and no LLM client is wired — converted output is treated as untrusted data and emitted verbatim.

**Child work items** (`t3 <overlay> ticket create-sub --parent <url> --title <…> [--type Task|Incident|Issue]`): the sibling of `ticket comment`, resolving the code host per-URL across overlays and delegating to `CodeHostBackend.create_sub_issue`. On GitLab this folds the three error-prone hops into one — REST create, `workItemConvert` to the child type (an Issue→Issue parent link is forbidden, so the default `Task` is the natural sub-item), then `workItemUpdate hierarchyWidget.parentId` to nest it — returning the child IID + URL for chaining. GitHub child work items use a different API and are unsupported.

**Overlay contract check** (`t3 overlay contract-check --compose <paths>`) reads every `${VAR}` reference in compose files and fails if any is neither defaulted nor declared by core (`_declared_core_keys()`) or the active overlay (`OverlayBase.declared_env_keys()`).

**Teatree source resolution in overlays.** `[tool.uv.sources] teatree = { path = "../../souliane/teatree", editable = true }` is the committed default — no SHA pinning, no mode switching. CI clones teatree at the same relative path before `uv sync`. Local dev uses whatever is checked out locally.

---

## 9. Code Host Sync

`teatree.core.sync.sync_followup() → SyncResult` is platform-agnostic. Per-overlay it resolves the overlay's `CodeHostBackend`, fetches open PRs authored by the current user (incremental via cached `updated_after`), upserts tickets by `issue_url` (or PR URL if no issue linked), enriches non-draft PRs with pipeline + approvals + review threads, infers ticket state from PR data (`infer_state_from_prs()` advances forward only, never regresses — and a post-ship inferred state is routed through the shared DoD decision per § "Sync writers honor the same gate"), and detects merged PRs.

| PR data | Inferred state |
|---|---|
| Draft | `started` |
| Non-draft | `shipped` |
| Non-draft + review-requested or approvals > 0 | `in_review` |

Review threads are classified `waiting_reviewer` / `needs_reply` / `addressed`. Draft notes (GitLab) / pending reviews (GitHub) surface as a statusline `review_draft` prompt to publish.

Posting discipline (#1207): `t3 review post-comment` defaults to creating a DRAFT and DMs the user the link; the colleague-visible `--live` path is gated on a single-use, MR-URL-scoped `LivePostApproval` minted by `t3 review approve-live-post <mr-url> --slack-ts <ts>` after the Slack DM at that timestamp is verified (from the user, recent within 15 min, contains an explicit approval phrase). The historical immediate-post default is retired; CLI enforces draft-by-default rather than relying on prose discipline.

One-step authorization (#126): `t3 review authorize <repo>!<mr> --approver <id>` collapses the former two-command on-behalf dance — it records the durable `OnBehalfApproval` AND mints the matching single-use `LivePostApproval` in one call. The `post-comment --live` path consults one consolidated read-only decision (`review_authorize.resolve_live_authorization`); the two original commands remain for the Slack-ts verification path.

Verified-delivery notify wrapper ([#1181](https://github.com/souliane/teatree/issues/1181)): `teatree.messaging.notify_with_fallback` is the resilient bot→user DM egress — it tries the canonical `notify_user` path first and, on a transport `FAILED` (the silent-rc=1 class under [#1173](https://github.com/souliane/teatree/issues/1173)), falls back to a direct messaging-backend send round-trip verified before being treated as delivered (a `NOOP` is not recoverable). `BotPing.transport` records which path landed the DM, and `t3 <overlay> notify send` surfaces the failure reason on rc=1. Loop/CLI call sites route through this wrapper; `teatree.core` callers stay on `notify_user` (the module graph forbids a `core → messaging` edge).

Review-shape audit (#1206): `t3 review run <MR_URL>` is the read-only entry point reviewer sub-agents call before scanning a diff. It fetches MR metadata, classifies complexity, counts existing-review state (open discussions + draft notes + approvals), and emits a structured JSON summary so every reviewer starts from the same shape rather than improvising. The command never publishes — it stays outside the on-behalf surface. GitHub PR URLs return `unsupported_forge` (exit 2) deterministically until a parallel GitHub backend lands.

Structured-evidence gate (#1280): `t3 review post-comment` and `post-draft-note` refuse a finding whose body matches an "X is missing/wrong/broken/stale" pattern unless an accompanying `FindingEvidence` record (`--evidence-json '{...}'`) carries verified receipts — it passes only when `confidence='verified'` AND at least one verified-path field is non-empty (full schema in code). Implemented in `teatree.cli.review_evidence_gate`; runs alongside the on-behalf (#960), colleague-MR shape (#1114), and TODO-anchor (#1186) sibling gates inside `ReviewService._run_pre_publish_gates`.

Close-trailer scanner (#1398): `[teatree.publish_gates] ban_close_trailers_on_namespaces` lists fnmatch patterns over `namespace/repo`. When the target PR/MR's repo matches and the body carries a `Closes|Fixes|Resolves` trailer (`part of` and full-URL variants too), `ShipExecutor._build_pr_spec` silently strips those lines before opening the PR (`teatree.core.close_trailer_scanner`). Distinct from the overlay-scoped `forbid_close_keywords` gate (#1012), which refuses the publish; this scanner cleans the body and proceeds.

Open-questions warn (#1933): any open question (solved or not) and any non-explicit assumption must be listed in BOTH the commit body AND the PR description under an `Open questions & assumptions` section (per-item status `decided-by-user` / `assumed` / `open`). `teatree.core.open_questions_gate` runs at both PR-creation chokepoints (`ShipExecutor._build_pr_spec`, orphan-branch `create_or_defer_pr`): a body lacking the heading warns and proceeds. Warn-only per the "a gate without a reliable heuristic warns" rule — the heading wording is not separable enough to block on. Canonical doctrine in `skills/ship/SKILL.md` § 5.

Egress-leak gate family — one doctrine, several entry points, each named here with its load-bearing invariant; detector lists, exit codes, thresholds, and exempt sets live in the code:

- **Public-repo diff privacy-scan** (#685, #730): pre-push hook `refuse-public-push-with-leak.sh` runs `t3 tool privacy-scan` over the pushed diff + commit messages when `origin` is PUBLIC, blocking emails / home paths / private IPs / keys / internal hostnames / banned terms. It fails CLOSED on a genuine finding (a dedicated exit code) but fails OPEN on any other non-zero so a scanner crash cannot wedge every push (#126).
- **Banned-terms posting gate** (#1415, `teatree.hooks.banned_terms_scanner`): the `PreToolUse` non-commit sibling, scanning gh/glab post bodies.
- **Pre-dispatch quote-scanner** (#1401, `handle_dispatch_prompt_quote_scanner`): the dispatch-boundary companion to #1213 — denies only a HIGH user-voice/PII match in an `Agent`/`Task` prompt, with a `[quote-ok: <reason>]` opt-out, so a verbatim quote cannot leak downstream.
- **Diff comment detectors** (added-lines-only): `code_comment_self_reference` (#1465) is a **blocking** `privacy_scan.py` diff detector (**fail-open per detector**, #1536) for bookkeeping self-references. `code_comment_density` (#1538) is the commit-side half of the near-zero-comments rule (#1532); it is **advisory** — the standalone check (#1369, `t3 tool comment-density`), the pre-push hook, and the `comment-density-warning` CI job warn and **exit 0**, never blocking, and it is NOT a `privacy_scan.py` blocking detector (#1844 — no content-blind "overly long prose" heuristic spares legit long comments). A golden corpus (`tests/test_comment_density_gate.py`) and an eval pin must-DENY symmetric with must-ALLOW. The push-stage gates (`doc-update-gate`, `ensure-pr`, the leak gate) are `stages: [push]` — skipped by a bare `prek run --all-files` but re-run by CI — so `t3 tool verify-gates` runs both stages and returns the combined exit code; skills and the builder prompt point at it so local green == CI green.
- **Full-tree banned-brand backstop** (#1570, `core.banned_terms_tree`, CLI `t3 banned-terms scan-tree`, CI job `banned-terms-tree`): catches what the diff/payload gates structurally cannot — a brand ALREADY committed, invisible to any post-landing diff — by scanning every tracked file's content with an underscore-tolerant boundary.

---

## 10. Configuration

The resolved-order config chain (`~/.teatree.toml` global → `[overlays.<name>]` override → env), Django settings, `OverlayConfig` methods, logging, data storage, and the state-placement rule (cache vs intent, #628) live in [docs/blueprint/configuration.md](docs/blueprint/configuration.md). Its override table also documents the per-overlay knobs: `mr_title_regex` (#1540, the Conventional-Commits title pattern the `pr create` gate enforces, no `--force` bypass), the `autonomy` switch (#1668, collapsing those gates into one value), and the `speed` throughput dial (`slow < medium < full < boost`, orthogonal to `mode`/`autonomy`). The `### 10.1 ~/.teatree.toml` subsection cited from `commands/followup.py` is preserved there.

Local text-to-speech ([#1791](https://github.com/souliane/teatree/issues/1791)): `teatree.core.speak.speak()` is the seam, gated on the macOS `say` binary and driven by the per-overlay `speak_mode`/`speak_target` pair. The `SpeakMode`/`SpeakTarget` enum docstrings (`teatree.types`) define the values and egress points; appendix §10.1.1 covers config, the Slack-audio scope, and CLI usage.

---

## 11. Skills & Plugin Architecture

Skills live in `skills/*/` — one `SKILL.md` + optional `references/` per skill. When installed as a plugin, skills are namespaced under `t3:` (e.g. `/t3:code`). The lifecycle skill set is `code`, `debug`, `test`, `review`, `review-request`, `ship`, `ticket`, `workspace`, `followup`, `handover`, `next`, `retro`, `contribute`, `setup`, `platforms`, `rules`. Read-only auxiliary skills sit alongside it: `checking` (what-did-I-miss) and `todos` (the current session's task list, via `tasks list --session`).

Skills declare dependencies via YAML frontmatter `requires:` (transitive, topo-sorted) and optional `companions:` (best-effort, warn on miss). Third-party skill frameworks (e.g. superpowers) are absorbed into the `rules` skill rather than delegated, to avoid context duplication.

**Sub-agents (`agents/`).** Eight phase agents wrap skill bundles: `orchestrator`, `coder`, `tester`, `e2e`, `reviewer`, `shipper`, `debugger`, `followup`. Each is a YAML+description wrapper that references skills via `skills:` frontmatter — no content duplication. Phase agents are invoked by lifecycle skills, by the headless executor (§5.2) when a phase task is claimed, and by the loop tick (§5.6) when a scanner signal calls for agent judgment. Interactive-only skills (no agent): `retro`, `next`, `contribute`, `setup`.

**Distribution.** Two install paths, one source of truth:

- **APM**: `apm install souliane/teatree`
- **CLI-first**: `git clone … && uv tool install --editable . && t3 setup` — also creates the plugin symlink `~/.claude/plugins/t3 → <clone>`

On every `t3 setup` run, `dep_drift` checks `[project].dependencies` against the editable install and reinstalls + `execv`-restarts if a declared dep is missing. The same run re-syncs runtime skill links and **prunes stale ones** — a teatree-managed link (broken, or still resolving under a managed core/overlay skills root) whose skill was removed or renamed upstream is removed so the dropped skill stops resolving; contribute-mode workspace links and a user's own real skill directories are left untouched. Because `t3 update` re-runs `t3 setup`, updating teatree auto-cleans skills dropped upstream.

**§11.4 Bash Permissions.** The plugin ships a **broad allow, narrow deny** `permissions.allow` list — every tool family the workflow legitimately touches is allowed, with load-bearing denies (`git push` to default branches, `--force`, `--no-verify`, `rm -rf` rooted at `/`, `~`, `.`, `..`, `curl|bash`, `gh repo delete`, etc.) taking precedence. The `t3` CLI is the workflow's safety wrapper — blocking inside the CLI is the wrong layer.

**Plugin config is not self-modifiable by the agent.** Edits to the plugin's `settings.json` allow-list are rejected by Claude Code's autonomy guardrail. When the classifier denies a tool call mid-workflow the agent must stop and ask via `AskUserQuestion`. The full protocol lives in `skills/rules/SKILL.md` § "Classifier Denial Protocol".

`t3 doctor authorizations` is read-only — it detects which generic recommended auto-mode authorizations are absent from the user's `~/.claude/settings.json` and prints the paste-ready sentence. Teatree ships **no** classifier whitelist of its own; recommendations only suggest.

---

## 12. Testing

**>90% branch coverage, non-negotiable** (`fail_under = 93, branch = true`). Omits only migrations.

- In-memory SQLite (`:memory:`) for isolation and speed; `django_tasks.backends.immediate` for synchronous task execution
- `conftest.py` monkeypatches `HOME`, `XDG_*` to `tmp_path`, strips `GIT_*` env vars, isolates overlay env, resets backend + overlay caches between tests
- Tests mirror `src/` paths under `tests/teatree_core/`, `tests/teatree_agents/`, `tests/teatree_backends/`, `tests/teatree_loop/`, plus top-level cross-cutting suites
- New tests lean integration / E2E / functional — Django test client, `call_command`, real `git` under `tmp_path`. Unit tests are reserved for pure logic. Mock only unstoppable externals
- Core has no Playwright suite (no UI). Overlays declare their own via `get_e2e_config()`; `t3 <overlay> e2e {run,external,project}` runs them. `t3 <overlay> e2e post-evidence` ([#1409](https://github.com/souliane/teatree/issues/1409)) posts ONE structured evidence comment on the **ticket** (never the MR), validation-gated (env ∈ {dev, local} — the deployed-env rule is now machine-enforced, not prose-only — before ≠ after byte-hash anti-fake, commit known + tree clean) and idempotent on a hidden `(ticket, env)` marker (one per env, edited in place). Validators + posting live in the sibling `_e2e_evidence` module; on-behalf-gated (#960) like every colleague-visible post

---

## 13. Quality Gates

| Tool | What it checks |
|---|---|
| `pytest` + `pytest-cov` | >90% branch coverage |
| `ruff` | All rules enabled, specific ignores justified (`# noqa` requires approval) |
| `ty` | Static type checker with `error-on-warning = true` |
| `import-linter` | Transitive/laundered dependency boundaries tach's direct-edge model misses (`core → messaging` even via an intermediate module, substrate → `contrib`, mini-loop independence); config in `pyproject.toml [tool.importlinter]` |
| `codespell` | Spell check |
| `prek` | Runs all above on commit |

Key ruff decisions: ALL rules selected then specific ignores; D1xx disabled (no docstrings — self-documenting code); `from __future__ import annotations` banned (use native 3.13 syntax).

---

## 14. Django Project Workflows

Reference DB architecture, the import fallback chain (`DjangoDbImportConfig` strategy + selective fake-migration retry + post-import steps), DSLR integration, the worktree provisioning workflow (`worktree provision`), the server startup workflow (`worktree start`), and the state reconciler (`t3 workspace doctor`) are implementation details — they live in code under `teatree.core.runners`, `teatree.utils.db`, `teatree.utils.django_db`, `teatree.core.reconcile`, and the `lifecycle` / `worktree` / `workspace` management commands. The architectural contract:

- **One reference DB per overlay** (canonical control DB at `teatree.paths.CANONICAL_DB`; worktree-aware code auto-isolates onto a per-worktree DB under `~/.local/share/teatree-worktrees/<slug>/`)
- **Configurable import strategy per overlay** (`get_db_import_strategy(worktree) → DbImportStrategy | None`) — overlays declare *what* to import; core runs the engine
- **Migration retry with selective faking** for known-stuck migrations, declared per overlay
- **Post-DB steps** run after import (password reset, fixtures, …) — declared per overlay
- **State reconciler** (`t3 workspace doctor`) reconciles DB ↔ disk ↔ docker drift on demand

---

## 15. Dependencies

Runtime:

```toml
croniter>=6.2.2
django>=5.2,<6.1
django-fsm-2>=4
django-rich>=2.2
django-tasks>=0.9
django-tasks-db>=0.12
django-typer>=3.3
httpx>=0.27
tomlkit>=0.13
```

`croniter` parses the `[teatree.availability].windows` cron expressions (§5.6.3 / §17.1 invariant 9); `tomlkit` round-trips `~/.teatree.toml` for `t3 setup` and `t3 config` edits.

Optional extras (installed on demand):

```toml
notion = ["browser-cookie3>=0.20"]
slack  = ["slack-sdk>=3.35"]
```

Dev: `ruff`, `pytest`, `pytest-cov`, `pytest-django`, `ty`, `import-linter`, `prek`, `safety`, `typer`, `django-types`.

---

## 16. Key Conventions

- Python 3.13+. Use `X | Y` union syntax. Never `Optional`.
- `from __future__ import annotations` is banned.
- No docstrings on classes/methods by policy. Self-documenting code (names + types are the documentation).
- Management commands use `django-typer`, not `BaseCommand`.
- `DJANGO_SETTINGS_MODULE` is stripped from env when running `_managepy()` so the overlay's own settings win.
- **Port allocation is ephemeral (Non-Negotiable).** Host ports are auto-mapped by Docker at `worktree start`; never written to `.t3-cache/.t3-env.cache`, the DB, or any other persistent store. Inter-service traffic uses compose service DNS.
- Coverage omits only migrations.
- `claude -p` is headless (exits immediately). The user's interactive `/loop` session is the only persistent Claude Code session.
- Statusline state is rendered to a file (`${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt`, override via `TEATREE_STATUSLINE_FILE`) by the loop and `cat`-ed by the hook. The hook itself does no DB or network I/O.
- **Overlay-specific names must not appear in `src/teatree/` or `docs/`.** The CI grep gate (`scripts/hooks/check_no_overlay_leak.py`) enforces this — `BLUEPRINT § 1`. Forbidden terms are loaded at runtime from `$TEATREE_OVERLAY_LEAK_TERMS` or `~/.teatree.toml` `[overlay_leak].terms` so the public repo never holds tenant names.
- E2E tests use file-based SQLite (not `:memory:`) because Playwright spawns a separate server process.

---

## 17. The Self-Improving Factory Architecture

Teatree is a durable self-healing **and** self-improving development factory. This section is the lasting architectural reference — the umbrella under [#836](https://github.com/souliane/teatree/issues/836); each component below is a separately tracked ticket implemented as deterministic teatree code, not skill prose.

Why this architecture exists, observed repeatedly: durability comes from **enforcement encoded in code/structure**, not prose that decays. A rule kept in memory/skills and relied on by vigilance recurs anyway; the same rule encoded as a gate/test/hook does not. The invariants below are the structural form of that lesson — load-bearing, binding every change to teatree itself.

### 17.1 Invariants

1. **Two layers, never conflated.** *Self-healing* (independent review, draft-locks, recovery, gates) is the substrate. *Self-improvement* runs on top: each caught failure-class becomes the smallest enforcement artifact that makes the class structurally impossible. Self-improvement is **gated by** self-healing — the system is never changed in a way the healing layer cannot catch or roll back.

2. **The flywheel.** A defect (from diff review, OR the code-health loop on un-changed code, OR an orchestrator-noticed near-miss) → the orchestrator synthesises → output is the *smallest enforcement* (gate/test/hook), never a prose rule → the failure-class is extinct. A repeat failure whose only output is memory/prose is a flywheel failure.

3. **Topology.** The orchestrator is the synthesis brain (retro synthesis, code-health triage, enforcement escalation, merge/clear decisions). Sub-agents are sensors/hands emitting structured signal into durable state, never self-judging. Skills carry judgment/methodology; teatree code carries the deterministic loops, gates, and intake. Corollary: mechanics → code, judgment → skill.

4. **Blast-radius rule.** Substrate changes are draft-locked and need a recorded human sign-off, satisfied by EITHER a per-PR `MergeClear.human_authorizer` OR the overlay standing at `autonomy = full` (`t3 <overlay> autonomy set full`) — the owner's standing grant to merge substrate without a per-PR sign-off. Either way the agent runs the `t3 <overlay> ticket merge` keystone (§17.4.3); the human never performs the merge. The carve-out removes ONLY the per-PR sign-off — the floor (cold-review `reviewer != maker`, SHA-bind, CI-green, not-draft, never-lockout, privacy/leak scan) holds on every substrate merge; below `full` the `human_authorizer` stays mandatory.

5. **Durability discipline is load-bearing.** Durable task/state plus pre-compaction snapshots let the orchestrator brain survive compaction/restart; keep them.

6. **Enforcement over prose, as a standing audit.** Invariant 2 says the flywheel's *output* is a gate, never prose; this invariant makes the *standing posture* explicit. Every user behavioural directive ("you should do X", "the agent shouldn't Y") MUST be (a) codified in teatree and (b) enforced by deterministic code/gates wherever mechanizable; skill prose is reduced to the judgment that genuinely cannot be mechanized, so skills get **lighter** over time. This is a recurring retro/review responsibility, not a one-time conversion. The enforcement gate (§17.6 / [#850](https://github.com/souliane/teatree/issues/850)) turns a mechanizable rule into a gate; the recurring audit keeps reclassifying prose rules → code gates so the prose corpus shrinks. Retroactive backfill: [#855](https://github.com/souliane/teatree/issues/855).

7. **Consolidation over drift.** Behavior encoded outside the teatree framework — personal `settings.json` hooks, dotfiles automation, overlay-local ad-hoc config, personal memory guardrails — must be considered for promotion into teatree on every retro/review pass. Genuine per-instance variance must be modelled as a documented teatree setting or config knob; undocumented divergence silently drifts and violates invariant 2.

8. **All FSM state transitions go through the `t3` CLI.** The pre-condition and pre/post transition hooks are the coherence mechanism (ledger update, attestation-binding to the HEAD/workstream the phase was earned against, privacy/AI-signature scan, `mark_merged()`). Out-of-band state mutation — raw `gh pr merge` / `glab mr merge`, or hand-editing the phase ledger / FSM state — is prohibited and **mechanically guarded** (`hook_router._BLOCKED_COMMANDS`, the same hook layer as the draft-lock and structured-question gates — invariant 2: code, not prose). The keystone IN_REVIEW → MERGED transition this protects is §17.4; the gate placement making it non-bypassable is §17.6.3. The two sanctioned escapes for legitimately stale state — clearing a reused ticket's phase ledger, recording an independent reviewer attestation — are themselves `t3` commands (`lifecycle clear-ledger`, the hardened `lifecycle visit-phase … reviewing --agent-id`), never manual edits.

9. **Every user-directed question is captured — sync or durable — and reaches Slack.** A user-directed question must either (a) call `AskUserQuestion` with the user reachable this turn, or (b) be recorded as a `DeferredQuestion` row when the resolved availability mode is `away`. Mode resolution is a single deterministic precedence — unexpired manual override → live presence (a recent `UserPromptSubmit`, upgrade-only) → `[teatree.availability]` cron-window match → `present` (default) — exposed by `t3 availability`. Manual override is authoritative; a present user (a prompt within 15 min) upgrades a schedule `away` to `present` so an actively-typing user is never muted outside work hours. The away path never bypasses the §807 structured-question gate — it is a *sanctioned destination* for the same `AskUserQuestion` call, converted at the `PreToolUse` layer. Since the user reads Slack, every `AskUserQuestion` is mirrored to their DM (away mirrors before denying), and away→present auto-drains the backlog. **Live-turn escape (#189):** when the current turn is user-driven (a `UserPromptSubmit` for the same session within `LIVE_TURN_FRESHNESS` = 90 s, shorter than the 15-min `PRESENCE_FRESHNESS`), `handle_route_away_mode_question` passes through and the question renders in-client even under manual-away — this PRESERVES the invariant (the question reaches the user synchronously) and is what makes `/checking` work without an availability flip; autonomous/loop-driven turns are unaffected and still defer+mirror. Component: §17.3 C3.

10. **Orchestrator never executes work directly — every implementation, review, test, debug, and ship action is dispatched to a sub-agent.** The orchestrator's role is synthesis, classification, dispatch, and CLEAR issuance (invariant 3, §17.4.1, §17.8 clause 3); the *hands* are sub-agents (`t3:code`, `t3:review`, `t3:test`, `t3:debug`, `t3:shipper`, `/teatree-batch`'s singleton delivery sub-agent) and the durable loop (§17.4.3). The orchestrator inlining implementation work — even a "trivial" typo Edit/Write, a Bash run to re-do a sub-agent's job, a local test cycle dodging `t3:test`, or self-executed background work — is the named anti-pattern: it conflates judgment with execution (the conflation §17.4 forecloses for merges), denies the maker≠checker independence the flywheel depends on, and concentrates the compaction/restart risk the topology spreads across durable handoffs. **Narrow exceptions:** (a) read-only orientation in the orchestrator's own session — a `Read`/`Grep` to route the next dispatch, a `gh pr view` / `glab mr view` / `git status` to re-verify cross-agent state, an `AskUserQuestion`, sanctioned messaging-send/view; (b) the `t3 …` invocations the orchestrator owns (`MergeClear`, attestation, next-dispatch); (c) conversational replies that produce no repo mutation. Anything that *changes a file, mutates remote state, or does the substantive work of a phase* is sub-agent territory, full stop. This is mechanized as gate 2 in §17.6.4 (`handle_enforce_orchestrator_boundary`): a `PreToolUse` deny on a main-agent LONG/HEAVY foreground `Bash` command (test suite, build, dev server, long sleep, full-tree sweep), with `run_in_background: true` the escape hatch and a non-empty payload `agent_id` distinguishing sub-agent from main ([#115](https://github.com/souliane/teatree/issues/115)). The narrow scope (heavy Bash, not every Edit/Write) is deliberate; the broader delegate-only discipline stays a lifecycle-skill judgment. This and the sibling over-deny gates (skill-loading [#1488](https://github.com/souliane/teatree/issues/1488), protect-default-branch, validate-mr, block-uncovered-diff, `handle_block_edit_before_planned`) share one `_fail_open_or_deny` chokepoint with always-available escapes (`t3 <overlay> gate … disable` kill-switches in out-of-repo `~/.teatree.toml`, the master `danger_gate_fail_open` switch, and the never-denied `self_rescue.SELF_RESCUE_ALLOWLIST`); the PUBLIC-egress leak gate is excluded and stays fail-CLOSED. Detail + self-rescue regression tests in §17.6.4.

11. **Any interactive Claude Code session that mounts this teatree install MAY drain the `PendingChatInjection` queue.** The inbound-Slack bridge (#1014) records each user DM as a `PendingChatInjection` row; the `handle_inject_pending_chat` `UserPromptSubmit` hook drains unconsumed rows into the next prompt's `additionalContext`. Drain eligibility is **decoupled from loop ownership**: the autonomous `t3 loop start` session holding `_OWNER_LOOP` never receives `UserPromptSubmit` events, so a `_session_owns_loop` gate here is the wrong invariant — it prevents *every* user reply from reaching *any* interactive session. At-most-once delivery rides primitives orthogonal to ownership: the `PendingChatInjection.consume()` single-use durable transition (`UPDATE … WHERE consumed_at IS NULL`) and the `(overlay, slack_ts)` `UniqueConstraint` (the scanner can over-poll safely). The loop-owner gate is correct for the §5.6 self-pump (singleton) and stays there; it does **not** belong on inbound message drains, whose point is that queued replies reach the interactive session that *can* surface them.

12. **An outcome-claiming completion carries a resolvable artifact pointer, fail-closed.** The out-of-band completion surface (`tasks complete --note`) records externally-landed work. `teatree.core.completion_evidence` makes two SEPARATE judgments. (a) *Outcome assertion (trigger):* the note asserts an outcome when an outcome verb — `merged` / `posted` / `shipped` / `deployed` (the `OUTCOME_CLAIM_KINDS` set, plus synonyms like `landed` / `released`) — either CO-OCCURS with a context cue (a branch/MR/PR/issue/commit word, a deploy target, a `review` surface, a `via` connector, or a pointer/path-shaped token) OR is the NOTE-INITIAL bare verb (the terse phantom `merged` / `merged to main`); internal-work idioms (`merge conflict`, `not to merge yet`) are stripped first, and a verb LEADING a longer sentence with an object ("merged the two helper functions") is never gated. (b) *Resolvable pointer (evidence):* an asserting note MUST carry what an auditor can follow — a URL, a git SHA with a `commit`/`sha`/`rev`/`@` cue, an MR/PR/issue ref `!123` / `#123`, a note id, or a real path (rooted, extensioned, or a dotted module path anchored on `teatree.`/`src.`/`tests.`). A bare `a/b`, a cue-less hex/digit run (build number, or a hex word like `deadbeef`), and dotted prose (`the.thing.now`) do NOT count — a claim without backing — so `check_completion_evidence` refuses (`CompletionEvidenceError`, sibling of `DodLocalE2EError`). A completion with NO note, or asserting no outcome, is never gated — the structural form of the recurring "done claims require artifact evidence" rule (invariant 6).

### 17.2 The flywheel — 17.8 Orchestrator-as-keystone contract

The flywheel diagram, components (C1 Retro / C2 Code-health loop / C3 Availability), §17.4 Orchestrator-decides / loop-executes topology (role boundaries, per-diff `MergeClear` record, loop validation before merge, post-merge audit), §17.5 TODO-consolidation quick-wins triage, §17.6 Enforcement gate (anti-relaxation, sound tach module boundaries, gate placement, shipped gates — incl. the §17.6.4 plan-gate — now a structural FSM impossibility: `CODED` unreachable from `STARTED` without `PlanArtifact` + `plan()` advancing to `PLANNED`; `handle_block_edit_before_planned` PreToolUse hook as an earlier second layer; the three wall-clock session/clock proxy gates ([#1133](https://github.com/souliane/teatree/issues/1133)) retired — and the §17.6.4 doc-update gate, [#1461](https://github.com/souliane/teatree/issues/1461), pre-push prek hook + CI mirror that blocks a new top-level `t3` command / `SKILL.md` / `Ticket.State` / `LoopLease` / `MiniLoopMarker` without a paired README or BLUEPRINT diff, and the §17.6.4 no-commit sub-agent recorder, [#1205](https://github.com/souliane/teatree/issues/1205), a `SubagentStop` detection hook that records a `terminated_without_commit` signal when an isolation-worktree sub-agent ends on a work branch with 0 commits, and the two complementary enforcement evals — gate-liveness [#168](https://github.com/souliane/teatree/issues/168) proving gates fire on synthetic payloads, transcript-replay [#169](https://github.com/souliane/teatree/issues/169) proving invariants held in real runs, local-only and privacy-safe; eval run-history [#1160](https://github.com/souliane/teatree/issues/1160) persisting each `t3 eval run` into the `EvalRunRecord`/`EvalScenarioResult` baseline ledger read by `t3 eval history`), §17.7 Enforcement-over-prose as a standing audit, and §17.8 Orchestrator-as-keystone contract — all live in [docs/blueprint/factory-architecture.md](docs/blueprint/factory-architecture.md). Section headings (`### 17.2`–`### 17.8`, incl. `### 17.4.2`, `### 17.6.3`) are preserved there for cross-references.

**Anti-pattern catalog ([#166](https://github.com/souliane/teatree/issues/166)).** `src/teatree/quality/antipatterns.yaml` is the SSOT for recurring architectural anti-patterns; `teatree.quality.catalog` loads it and `scripts/hooks/generate_antipattern_catalog.py` renders [docs/generated/antipattern-catalog.md](docs/generated/antipattern-catalog.md). Each entry's `detection` tier (`greppable` vs `judgement`) feeds the three review tiers off the one catalog: design-time (`architecture-design`), per-PR deterministic (`check_antipatterns.py`, manual stage — gate promotion deferred), periodic holistic (`ac-reviewing-codebase`). `tests/quality/test_catalog.py` is the reachability ledger — every named `linter` resolves to a real hook/tool, every `eval_invariant` to a real transcript invariant. The sibling `teatree.quality.test_shape` check (`t3 tool test-shape`) flags N≥3 near-identical unparametrized test functions and a test:source ratio regression past the committed `[tool.teatree.test_shape]` baseline — advisory `warn` by default (never a PreToolUse gate), opt-in `block` via `mode`; a must-FLAG/must-NOT-FLAG golden corpus prevents false positives on parametrized or distinct tests.

**Scoped mutation testing ([#131](https://github.com/souliane/teatree/issues/131)).** `t3 mutation run` runs mutmut over only diff-touched modules in the NARROW `[tool.teatree.mutation]` safety registry; `tests/quality/test_mutation*.py` carry the ledger + kill-proof.

**Behavioral eval harness ([#1160](https://github.com/souliane/teatree/issues/1160)).** `src/teatree/eval/` grades agent behaviour from a `claude -p` run: matchers, pass@k, trigger-QA, a baseline run-store, a model matrix, an LLM judge, and a free regression corpus (`t3 eval regression`) pinning recurring failure classes on the real code path. A run + judge **default to the `claude-sonnet-4-6` tier** (`model:`/`judge.model:`/`--models` override) so a bare run keeps Opus quota free. Failure-class scenarios + `_pass`/`_fail`/`_noop` fixtures live in `scripts/eval/corpus_gen`, emitted by `generate_corpus.py` (drift-checked). **`t3 eval all` ([#1781](https://github.com/souliane/teatree/issues/1781))** runs all lanes in one table, with no silent metering (`--backend sdk` opts in); driver is `/t3:running-evals`. Schema + failure-class index in [src/teatree/eval/README.md](src/teatree/eval/README.md).

---

## Architectural Appendices

This file holds the architecture. Three appendices carry detail that is genuinely architectural but too long to inline:

| Appendix | Why it stays an appendix |
|---|---|
| [factory-architecture.md](docs/blueprint/factory-architecture.md) | §17.2–§17.8 — flywheel, components, orchestrator-decides / loop-executes topology, enforcement-gate family. Each subsection is cross-referenced from code (e.g. `hook_router.py` cites §17.4 / §17.6 / §17.8). |
| [loop-topology.md](docs/blueprint/loop-topology.md) | §5.6 deep mechanics — lease + owner-record interplay, scanner roster, three-stage tick, statusline rendering, availability dual-mode. Cited from `tests/test_blueprint_loop_epic_alignment.py`. |
| [configuration.md](docs/blueprint/configuration.md) | §10 — resolved-order config chain (`~/.teatree.toml` global → per-overlay → env), Django settings, `OverlayConfig` methods, logging, data storage, state-placement rule. `### 10.1` is cited from `commands/followup.py`; `### 11.4` recommended-authorizations from `cli/recommended_authorizations.py`. |

Implementation details that previously lived in nine prose-of-code appendices (`agent-execution`, `architecture-and-package`, `backends-and-sync`, `command-tiers`, `dependencies-and-conventions`, `django-workflows`, `domain-models`, `overlay-system`, `skills-testing-gates`) have been folded into the sections above or moved to their true home — model and `OverlayBase` docstrings, typer `--help` text, `CLAUDE.md` / `AGENTS.md`, or kept in code where they were always canonical. See [#1128](https://github.com/souliane/teatree/issues/1128).

---

## Maintenance

Two pre-commit gates keep this file architectural, not implementation prose:

- `scripts/hooks/check_blueprint_size.py` ([#1180](https://github.com/souliane/teatree/issues/1180)) hard-fails any commit touching this file when it exceeds 100 KB. To raise the cap for a planned, reviewed bump in the same commit, set `T3_BLUEPRINT_SIZE_OVERRIDE=1`.
- `scripts/hooks/check_blueprint_size_budget.py` ([#1128](https://github.com/souliane/teatree/issues/1128)) enforces soft byte budgets on the corpus when this file or any `docs/blueprint/*.md` appendix is staged: top-level `BLUEPRINT.md` 87,000 B, `docs/blueprint/` appendices 116,000 B, combined total 203,000 B. To raise a budget for a reviewed addition in the same commit, set `BLUEPRINT_SIZE_OVERRIDE=1`.

The auto-generated tach dependency graph lives in [docs/dependency-graph.md](docs/dependency-graph.md), outside both budgeted corpora, so structural growth never trips either gate.

---

## Module Dependency Graph

See [docs/dependency-graph.md](docs/dependency-graph.md) for the auto-generated graph.
