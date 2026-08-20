# TeaTree — Agent Instructions

This is the teatree repo — both the Python package (`src/teatree/`) and the workflow skills (`skills/*/`). You are developing teatree itself, not using it on a downstream project.

## First Principles (Non-Negotiable — outrank BLUEPRINT.md and every section below)

These are the owner's standing directives. Where anything else in this repo conflicts with them, they win.

1. **Zero regression allowed.** A fix without a regression test is not a fix. An AI *behavioural* fix without a regression **eval** is not a fix. Find the right surface to test: prefer **integration** tests over unit tests; reach for **evals** and **E2E** when those are what actually observe the behaviour. A test that passes on the buggy code guards nothing — observe it RED first.
2. **Code is improved continuously**, not just extended: factorized, clean, maintainable, robust, extensible, bullet-proof — with as **few comments as possible**. Comments as code: express intent in names, types and structure so prose is not needed. Comment only what the code genuinely cannot say (a non-obvious *why*, a measured constant, a deliberate divergence).
3. **Instructions are a fallback** for whatever cannot be ENFORCED deterministically. If a rule can be made mechanical, make it mechanical and delete the prose.
4. **Checks and gates are a safety net** for whatever could not be properly ENFORCED. They catch what the design failed to prevent; they are not the design.
5. **The factory requires as little human intervention as possible.** Every question asked of the owner is a cost — remove its cause where you can.
6. **The factory is RESILIENT: it auto-repairs and auto-improves itself.** A failure that needs a human to notice it is an unfinished failure.
7. **The factory is RELIABLE.** It does the same thing every time, and what it verifies is what it runs. A green check that proves a different environment than production is not a check — pin the versions, run the real path, and make the surface that ships the surface that was tested. Flakiness is a defect, not a condition to retry around.
8. **No known gap outlives the PR that revealed it.** A gap an agent becomes aware of that concerns what it is working on is closed in the SAME pull request. No follow-up issue, no "phase 2", no `TODO`, no deferral to the next ticket. Filing a gap you could have fixed is not progress — it is the work, moved.
9. **One phase, one PR per repository.** A plan has exactly one phase, and implements EVERYTHING in at most one PR per repo it touches. Do not propose staged rollouts, phase 1/2/3 sequencing, or a "foundation now, rest later" shape — if it is in the plan, it ships in that single PR.
10. **An agent terminates only when its knowledge is fully implemented.** On finishing, everything the agent knows needs doing is done — 100%, by that agent. Nothing understood is handed to somebody else, to a future session, or to the owner. "Out of scope" is not a place to put work you already know how to do.
11. **The owner's attention is the scarcest resource in the factory.** Assume the owner never reads Claude Code messages, and reads Slack only when forced to. Volume is what makes that true: every extra message lowers the odds the one that matters is read. So raise only what genuinely cannot be decided without a human, and decide and deliver everything else in silence.

Read 8, 9 and 10 together — they are one requirement seen at three moments. 9 is completeness at plan time, 8 at implementation time, 10 at exit. Each exists because the other two are evaded the same way: by naming known work "later". There is no later.

**What 8-10 bind — and what they do not.** They bind *deferral*. They do not widen scope, and they grant no authority:

- **Scope.** They reach what the change in front of you touches — what it leaves broken, half-done, or contradicted. A gap uncovered while closing one is in scope only if it sits on that same surface; past it, name the finding in the PR body and stop. "Everything the agent knows needs doing" means everything in scope, not everything it noticed. Closing a gap must terminate, so the surface is the bound.
- **Authority.** Maker is still not checker: a reviewer reports what it finds and never fixes it (§ "Reused-ticket attestation" below; `skills/review/SKILL.md`, `skills/ship/SKILL.md` § "Merging is the §17.4 keystone transition", `skills/wip/SKILL.md` § "Hard rails parallelization must not break"). Every recorded approval gate still holds — issue creation and plan/memory files (both below), remote-DB writes, on-behalf and live posts, review requests, merges, the E2E bypass. Evals, tests and the ship gates are never skipped to satisfy a principle. An agent stopped at a gate has finished by asking; "do it now" is not consent.

Read 3 and 4 together, and mind *who acts*:

- **When the actor is deterministic** (code, CI, a hook), enforce it mechanically. A rule that can be made impossible to break should be impossible to break, and the prose restating it should then be deleted.
- **When the actor is an AI**, the instruction comes FIRST. A gate does not prevent an agent from doing the wrong thing — it fires after the fact, so every trigger costs a whole repair cycle in tokens and wall-clock. The instruction is what makes the behaviour right on the first attempt; the gate only catches what the instruction failed to convey.

So a gate is never the answer to "how do we make the agent do X". It is the net under X, sized for the times the instruction did not land. Prose that restates what a mechanism already makes impossible is duplication; prose that shapes agent behaviour is the cheapest control there is.

## Repo Change Safety

- Never create a new plan file, memory file, journal file, or repo-instruction file in this repository without the user's explicit approval first.
- This includes files under paths such as `docs/plans/`, ad-hoc notes, repo-local memory artifacts, and new instruction/config files meant only for the agent.
- If a workflow or skill says to write such a file, stop and ask the user before doing it. Repo policy wins.

### Retiring/renaming a symbol: grep the FULL consumer set (Non-Negotiable)

Amplifies CLAUDE.md "No stale references". When a change retires or renames a function/field/registry-key/directive, the consumer set is **not just the handler/model that defines it**. Before claiming "no stale references / no dead code", grep across at minimum: (1) user-facing CLI help/docstrings and Typer `help=` strings; (2) generated docs (`docs/generated/*` — regenerate via the doc-generator/hook, never hand-edit); (3) PreCompact/snapshot/registry-serialization code that may read a now-unwritten field as a permanently-dead branch; (4) test fixtures that still write the retired shape (a green test guarding retired behavior is a stale consumer). A "retire X" PR/commit body asserting completeness without this grep is a false claim — it will bounce at cold review.

### Reused-ticket attestation: the gate-pass is NOT the guarantee (Non-Negotiable)

`t3 teatree workspace ticket <url>` is idempotent on `issue_url` (one ticket per issue) and does **not** clear the ticket's aggregated phase ledger — `Ticket.aggregate_phase_records()` unions `visited_phases` across ALL the ticket's sessions. A prior workstream's `testing/reviewing` therefore aggregates into the next workstream's view of the ledger; the structural guard is the `Ticket.reopen()` FSM transition (#1286), which retires every session's `visited_phases`/`phase_visits`/`repos_modified`/`repos_tested` so the next workstream re-earns its attestations from scratch. `reopen()` is the FSM-internal workstream-boundary call; the sanctioned `lifecycle clear-ledger --confirm` CLI is the operator-driven equivalent when a reuse path bypasses `reopen()` (e.g. an in-state reuse off SHIPPED without an explicit reopen). Even with the ledger-retire, the coordinator integrity guarantee for multi-workstream / reused-ticket work stays the same defense-in-depth chain: **(a) coordinator-orchestrated independent cold review of THIS workstream's exact diff by a freshly-spawned reviewer sub-agent that has not seen the implementation (the spawn boundary is the independence, not a stored identity), (b) coordinator verification, (c) explicit per-workstream coordinator CLEAR to the review-loop** — recorded as the durable attestation receipt. Never chain attest→pr-create→merge on a pre-review gate-pass; STOP-for-review at pr-create and let the coordinator orchestrate review→CLEAR.

## Issue Creation (Non-Negotiable)

- **Never create issues without explicit user approval.** Always ask first — present the title and a summary, let the user decide.
- **Search the open backlog first, and reuse a host issue where one fits.** `gh issue list` returns 30 rows by default, so a first-page read reports "nothing open matches this" while the host sits at position 40 — pass `--limit 200`. Extending beats filing a near-duplicate: append the new evidence and acceptance criteria to the existing issue's body or as a comment, so the host carries the whole ask. A cross-link from a second issue is not reuse.
- **One issue per root cause, not per finding.** Findings that a single PR would close belong in one issue. The exception is scope, not similarity: a genuinely unrelated defect in another subsystem still gets its own issue — this rule bounds duplication, never the backlog's coverage.
- **Teatree is a public repository.** Only generic, project-agnostic issues belong here. Never mention downstream project names, tenant names, customer names, internal architecture, feature flags, or any proprietary information.
- **Overlay-specific issues go on the overlay repository.** If an issue involves both core teatree and an overlay, create it on the overlay repo and reference the core component — not the other way around.
- **When in doubt, ask.** If you're unsure whether an issue is generic or overlay-specific, ask the user before creating it anywhere.
- **Link commits to issues.** When fixing a tracked issue, use `Fixes #<number>` or `Closes #<number>` in the commit message body (not the first line) to auto-close it on merge. Use `Relates-to #<number>` for partial progress.

## What TeaTree Is

A personal code factory for multi-repo projects. It turns a ticket URL into a merged pull request by coordinating worktrees, databases, ports, AI agents, and code-host sync across every repo the ticket touches. Target: service-oriented projects with databases and CI pipelines (any language). Not for docs-only repos or CLI tools.

It provides:

- A unified CLI (`t3`) for worktree creation, provisioning, dev servers, CI, and delivery
- A Django app (`teatree.core`) whose delivery lifecycle runs on four `django-fsm` state machines (`Ticket`, `Worktree`, `Task`, `PullRequest`), plus supporting models (`Session`, `TaskAttempt`, `TicketTransition`, event/intent/merge-clear records)
- An overlay system for downstream project customization (`OverlayBase`)
- Backend protocols for pluggable external integrations
- Agent workflow skills (`skills/*/`) for the full development lifecycle
- A statusline-based monitoring surface for tickets, PRs, and agent sessions

## Repo Layout

```
src/teatree/           Python package (the Django app + CLI)
  cli/                 Typer CLI package — the `t3` entry point
  config/              Settings resolution (DB ConfigSetting store), setting-home registry,
                       overlay discovery, and `defaults.toml` — the shipped-defaults authority
  skill_support/       Skill selection policy (`loading.py` — phase → skills, cwd detection),
                       transitive `requires` / soft `companions` resolution (`deps.py`),
                       and the `agents/*.md` frontmatter reader (`agent_declarations.py`)
  core/                Django app: models, managers, views, selectors, management commands
    models/            Model package — Ticket/Worktree/Task/PullRequest (FSM) + Session, TaskAttempt, TicketTransition, etc.
    selectors/         Selector functions (no domain logic in views)
    backend_protocols.py  Protocol classes (CodeHostBackend, CIService, MessagingBackend)
    overlay.py         OverlayBase ABC — extension point for downstream projects
    overlay_loader.py  Discovers the active overlay class from `teatree.overlays` entry points
    management/commands/  Django-typer commands (lifecycle, workspace, db, run, followup, pr, tasks)
    views/             Admin views
    templates/         Django admin templates
    <leaf packages>    cleanup/ worktree/ provision/ factory/ intake/ review/ evidence/ merge/ gates/ …
  backends/            Pluggable service integrations, one package per forge
    loader.py          Overlay-config-driven backend resolution (cached via lru_cache)
    github/            GitHub code-host client + `sync.py` (GitHubSyncBackend, the SyncBackend ABC from teatree.types)
    gitlab/            GitLab code-host client, CI pipeline operations, per-concern sync modules
    slack/, notion/, msteams/, sentry.py, sharepoint.py, figma.py   Other integrations
  agents/              Agent runtime
    runner.py          Headless in-process runs behind the `Harness` seam (structured JSON envelope)
    harness.py         The `Harness` / `HarnessSession` protocols the runtimes implement
    handover.py        Headless ↔ interactive session handover (resume by session id)
    model_tiering.py   Per-phase model override resolution
    skill_bundle.py    Skill dependency resolver for agent launch
    prompt.py          System context and task prompt builders (headless + interactive)
    attempt_recorder.py  Shared schema + evidence gate applied to every recorded attempt
    result_schema.py   JSON schema for structured agent output
  utils/               Git helpers, port allocation, subprocess wrappers
  overlay_init/        `t3 startoverlay` templates (overlay package + app)
skills/*/              Workflow skills (SKILL.md + references/)
tests/                 Pytest suite (>=93% coverage required)
scripts/               Standalone Python CLI scripts
hooks/                 Agent platform hooks (Claude Code hook_router, statusline, etc.)
```

## Models

The models live in the `teatree.core.models` package (one module per model
group). Four carry `django-fsm` state machines; the rest are supporting
records.

### Ticket — Core delivery entity (FSM)

- **States:** not_started → scoped → started → coded → tested → reviewed → shipped → in_review → merged → retrospected → delivered (plus `ignored` for abandoned tickets)
- **Fields:** overlay, issue_url, variant, repos (JSONField), state (FSMField), role, extra (JSONField)
- **Key methods:** scope(), start(), code(), test(), review(), ship(), rework()

### Worktree — Per-repo lifecycle (FSM, FK → Ticket)

- **States:** created → provisioned → services_up → ready
- **Fields:** overlay, ticket (FK), repo_path, branch, state (FSMField), db_name, extra (JSONField) — the on-disk path and allocated ports live under `extra`, not as dedicated columns
- **Key methods:** provision(), start_services(), verify(), db_refresh(), teardown()

### Task — Agent work unit (FSM, FK → Ticket, Session)

- **Fields:** ticket (FK), session (FK), parent_task (self FK), phase, execution_reason, failure_reason + failure_kind, status (FSMField: pending/claimed/completed/failed)
- `execution_reason` is why the task was SCHEDULED; `failure_reason` is why it FAILED, named by `core/task_failure_taxonomy.py`. `Task.fail()` requires a reason, so no failure path can land a task in FAILED with no cause attached.
- **Claim/lease:** claimed_at, claimed_by, lease_expires_at, heartbeat_at, result_artifact_path
- **Key methods:** claim(), complete(), fail(), reopen(), park()

### PullRequest — PR/MR lifecycle (FSM)

- **States:** open → review_requested → approved → merged
- **Key methods:** request_review(), approve(), mark_merged()

### Session — Quality gate tracker (FK → Ticket)

- Tracks visited phases across tasks within a conversation (not FSM-driven)
- **Fields:** overlay, ticket (FK), visited_phases (JSONField), phase_visits (JSONField), started_at, ended_at, agent_id, repos_modified, repos_tested
- Quality gates enforce ordering: reviewing requires testing, shipping requires reviewing
- **Terminal point:** `ended_at` is written by `Session.close()`, driven from the `_close_session_on_terminal_task` `post_save` receiver — a Task reaching COMPLETED/FAILED closes its session once no sibling task on it is still active. `SessionQuerySet.live()` bounds the open-session liveness signal by `session_stale_after_hours` so a crashed agent cannot pin its ticket busy forever

### TaskAttempt — Execution history (FK → Task)

- **Fields:** task (FK), started_at, ended_at, error, exit_code, artifact_path, result (JSONField), input_tokens, output_tokens, cost_usd, num_turns, launch_url, agent_session_id
- Enables cross-task failure querying and audit trail

Other supporting models include `TicketTransition` (phase-change log),
`IncomingEvent` / `IntentClassification` (event routing), and the
`MergeClear` / `MergeAudit` family.

### New model queried on the always-run path (Non-Negotiable)

When a new model is read by code that runs on every loop tick or at
import/startup — loop scanners registered in `build_default_jobs`,
signal handlers, statusline builders — that query MUST tolerate a
missing table. A fresh or pre-migration install runs the code before
`migrate` creates the table; an unguarded queryset turns into a
per-tick error for *every* user until they migrate. Materialise the
queryset inside `except (OperationalError, ProgrammingError): return
<empty>` — narrow to the missing-relation classes so a genuine DB
outage still surfaces via `_run_job`. Canonical exemplars:
`IncomingEventsScanner.scan`, `_reap_stale_task_claims`.

## Three-Tier Command Split

| Tier | Tool | Examples | Needs Django? |
|------|------|----------|---------------|
| Runtime commands | Django management commands (django-typer) | `worktree provision`, `tasks work-next`, `followup refresh`, `loop_tick` | Yes |
| Bootstrap commands | `t3` Typer CLI | `t3 startoverlay`, `t3 agent`, `t3 info`, `t3 loop start/stop/status` | No |
| Internal utilities | Python modules in `utils/` | Port allocation, git helpers, DB ops | Imported by commands |

### Deciding Where a New Command Lives (Non-Negotiable)

**Rule: anything that touches the Django ORM — models, querysets, `apps.get_model()`, inline `from teatree.core.models import ...` — MUST be a Django management command** in `core/management/commands/`, not a plain Typer command with manual `django.setup()`.

Why: manual `django.setup()` in CLI modules causes module-level import chains that pull in Django models before Django is bootstrapped. This breaks test isolation (test hangs, real backend imports) and is architecturally wrong — the management command framework exists to solve this.

**Pattern for CLI → management command delegation:**

1. Create the management command in `core/management/commands/<name>.py` using `TyperCommand` from `django-typer`. All heavy imports (backends, ORM, scanners) go here — **inline in `handle()`**, not at module level.
2. The CLI command in `cli/<group>.py` stays thin: it calls `django.setup()` + `call_command("<name>", ...)`. The CLI module imports **nothing** from `core/` or `backends/` at module level.
3. Tests for the management command use `call_command()` with `django.test.TestCase`. Tests for the CLI wrapper (if any) test only the delegation, not the business logic.
4. To abort a `TyperCommand` subcommand with a nonzero exit, `raise SystemExit(1)` — **not** `typer.Exit(1)` and not `sys.exit(1)`. Under `call_command`/django-typer the `typer.Exit` return-code path hits `'int' object has no attribute 'endswith'`. `raise SystemExit(1)` is the sibling convention (`tasks.py`, `e2e.py`, `overlay.py`) and is what `pytest.raises(SystemExit)` (the project convention for refusal tests) expects.

**Example — `t3 loop tick`:**

```
cli/loop.py (thin)          →  django.setup() + call_command("loop_tick", ...)
core/management/commands/loop_tick.py  →  all tick logic, inline ORM imports
```

**Overlay commands** (`t3 <overlay> worktree provision`, etc.) use a different delegation mechanism: `managepy()` runs `python manage.py <cmd>` in a subprocess via `OverlayAppBuilder`. Cross-overlay commands (like `loop_tick`) use in-process `call_command` instead.

**When NOT to use a management command:** commands that never touch Django — `t3 info`, `t3 startoverlay`, `t3 loop start/stop/status`, `t3 slack listen`. These are pure Typer commands in `cli/`.

## Overlay System

An overlay is a lightweight Python package that customizes teatree. It:

1. Subclasses `OverlayBase` (from `teatree.core.overlay`)
2. Implements mandatory hooks: `get_repos()`, `get_provision_steps(worktree)`
3. Optionally implements: `get_env_extra()`, `get_run_commands()`, `get_db_import_strategy()`, `get_post_db_steps()`, `get_symlinks()`, `get_services_config()`, `can_auto_merge()`, `get_workspace_repos()`. Project metadata hooks (`validate_pr()`, `get_skill_metadata()`, `get_followup_repos()`, `get_ci_project_path()`, `get_e2e_config()`, `detect_variant()`) live on the composed `OverlayMetadata` (`overlay.metadata`); credentials/URLs live on `OverlayConfig` (`overlay.config`)
4. Registers via a `teatree.overlays` entry point in `pyproject.toml` (e.g., `my-overlay = "myapp.overlay:MyOverlay"`)
5. Gets auto-discovered by the overlay loader from `importlib.metadata.entry_points(group="teatree.overlays")`

### Overlay API version (`teatree.__overlay_api_version__`)

Teatree exports `__overlay_api_version__` (a string) for overlays to assert against at import time. Bump it on every **breaking** change to the overlay-facing surface — `OverlayBase` method signatures, `Worktree`/`Ticket` fields overlays read, the entry-point contract, or runner protocols overlays may implement. Non-breaking additions (new optional hook, new helper) do not bump it.

Overlays should hard-fail at import (no shim, no deprecation warning) when the runtime teatree exposes a different version than what they were built against. CI catches the rest before merge.

## Backend Architecture

### API Protocols (`core/backend_protocols.py`)

Each external API concern is a `@runtime_checkable Protocol` in `teatree.core.backend_protocols`:

| Protocol | Purpose |
|---|---|
| `CodeHostBackend` | PR/MR creation, list own/review-requested PRs, PR comments, issue fetch + comments, file upload |
| `CIService` | Pipeline cancel, errors, failed tests, trigger, quality check |
| `MessagingBackend` | Mentions, DMs, posts, replies, reactions, user-id resolution |

Backends are auto-configured from overlay methods. For example, `get_gitlab_token()` and `get_gitlab_url()` on the overlay class drive the GitLab backend; `get_slack_token()` and `get_review_channel()` drive Slack. No individual `TEATREE_*` Django settings are needed — each overlay carries its own configuration.

### Overlay Methods That Wrap Platform APIs Belong on a Backend

Overlay extension points are for **project-shaped** values: which repos exist, how to provision a worktree, which CI project to query, what tenants are valid. They are **not** for platform-API wrappers (HTTP, `gh`/`glab` shellouts, SDK clients). When an overlay method's body contains an HTTP call, a `subprocess` call to a platform CLI, or an SDK client instantiation, that's a sign the logic should move to (or delegate to) a core backend protocol.

Concretely:

- **Overlay** answers "which GitLab project is this overlay's CI?" (returns a string) — that's overlay-shaped.
- **Backend** answers "fetch the title of this issue" or "list my open MRs" (calls the GitLab API) — that's backend-shaped, and a protocol method like `CodeHostBackend.get_issue` or `CodeHostBackend.list_my_prs` already exists.

When adding a new overlay method, ask: would two overlays end up implementing this against the same API? If yes, write it once on the backend protocol and have overlays consume it. The periodic holistic review (`ac-reviewing-codebase` § 1) enforces this rule during audits, via the catalog entry `plugin-wraps-platform-api`.

### Sync ABC (`teatree.types`)

Every file under `backends/` that syncs external data into the Django DB must subclass `SyncBackend` from `teatree.types` (the followup driver `sync_followup()` lives in `teatree.core.sync`):

```python
class SyncBackend(ABC):
    @abstractmethod
    def is_configured(self, overlay: object) -> bool: ...  # has credentials?
    @abstractmethod
    def sync(self, overlay: object) -> SyncResult: ...     # run the sync
```

Convention: `sync()` and `is_configured()` are instance methods decorated with `@override`. All internal helpers are `@classmethod`. No module-level functions in backend files — all logic lives on the class.

Current implementations: `GitHubSyncBackend` (`backends/github/sync.py`), `GitLabSyncBackend` (`backends/gitlab/sync.py`).

## Agent Runtime

Two lanes, one per task kind. Per-phase model overrides come from
`src/teatree/agents/model_tiering.py` in both.

### Headless Sessions (`src/teatree/agents/runner.py`)

Headless tasks drive an in-process agent session behind the `Harness` seam
(`src/teatree/agents/harness.py`), whose backend `agent_harness` selects — `claude_sdk`
(a `claude-agent-sdk` `ClaudeSDKClient`) or `pydantic_ai` (an OpenAI-compatible
endpoint). Both yield the same `AssistantMessage` / `ResultMessage` vocabulary,
so the driver never special-cases the transport.

- Collects the typed messages the session yields and validates the result envelope against `result_schema.py`
- If the result carries `needs_user_input: true`, reroutes the task to the user-input queue
- Stores the parsed result in `TaskAttempt.result`
- **Session resume:** when a `parent_task` chain carries a previous `agent_session_id`, the harness opens the session with the SDK-native `resume=` option (`pydantic_ai` rehydrates the equivalent message history from `src/teatree/agents/pydantic_ai_resume.py`).

### Skill Loading

Skills in `skills/*/` are loaded via the plugin system (see `hooks/hooks.json`) or installed as symlinks into agent skill directories. Skills with "Auto-loaded as a dependency" descriptions are not user-invocable — loaded via `requires:` in other skills' frontmatter.

## Statusline

The persistent UI surface is a multi-line statusline rendered by `t3 loop tick`. Each zone (action_needed, in_flight, info) gets a distinct color. PR URLs render as terminal hyperlinks (OSC 8). `t3 loop status` shows the last-rendered output.

## Development Workflow

### Running

```bash
t3 --help                           # CLI help
t3 acme agent                       # Launch Claude Code with overlay context
t3 agent                            # Launch Claude Code (teatree-self development)
```

### Testing

```bash
bash dev/test-affected.sh           # the diff-scoped lane — the local default (`--full` opts into the whole suite)
bash dev/ci-parity-fast.sh          # pre-push inner loop: scoped prek + makemigrations + affected tests + push gate
bash dev/test-cov.sh                # Coverage lane: parallel + --cov --doctest-modules, 93% floor (CI parity)
prek run --all-files                # Commit-stage hooks ONLY (ruff, codespell, tach, ty)
t3 tool verify-gates                # FULL CI-parity gate set: commit AND push stages
bash dev/test-fast.sh               # Declared exception: whole host suite, Python 3.13, parallel
bash dev/test-matrix.sh             # Declared exception: Docker matrix, Python 3.13 + 3.14
```

**The local default is diff-scoped; the whole suite is a declared exception ([#3994](https://github.com/souliane/teatree/issues/3994)).** A local whole-tree sweep pays a second time for the run CI's required sharded lane is about to make, and it is on the critical path — measured as most of a 3.5h ticket cycle against a 30m target. `dev/test-affected.sh` is fail-safe TO FULL (a migration, a conftest / `factories.py` / test-settings edit, an unclassifiable path, a missing merge-base, or a change to the selection machinery itself each run everything on their own), so scoping never under-runs. Reach for `--full` / `dev/test-fast.sh` / `dev/ci-parity.sh` when a change is genuinely cross-cutting and declare it; CI's sharded `test (3.13)` lane and the 93% whole-tree floor remain the merge authority either way. `teatree.quality.local_verification` pins that every per-phase mandate surface names the scoped lane.

**The push path does NOT run the test suite — push -> CI is the gate.** The full suite is CI's job, never the local push path: a host under load times out unrelated wall-clock and concurrency tests and blocks an otherwise-good push (#112/#21/#38). The pre-push hooks are fast, scoped gates only (public-repo leak refusal, doc-update, comment-density, ensure-pr). Guarded by `tests/test_no_full_suite_on_pre_push.py` so a full-suite hook can't silently regress back onto pre-push.

**`dev/test-fast.sh` and `dev/test-matrix.sh` are explicit opt-in local runners, not the push path.** Run `dev/test-fast.sh` when you want the host suite locally on Python 3.13. Run `dev/test-matrix.sh` before merges that touch `dev/Dockerfile.test`, `uv.lock`, or system dependencies: it runs the suite in Docker across Python 3.13 + 3.14 and catches missing system dependencies and Python-version-specific differences the host gate can't. If the Dockerfile changed, remove the cached image first: `docker rmi teatree-test`.

**The CI `test (3.13)` gate is sharded 4-way behind an unchanged combiner.** The heavy lane is a `test-shard` matrix (`pytest-split --splits 4 --group N`, `-n auto` within each shard) that measures coverage but enforces NO floor; the `test` COMBINER job aggregates them — it fails if any shard failed, asserts the shards partition the suite exactly once (`scripts/ci/check_shard_completeness.py`), then combines the coverage and enforces the 93% floor + per-module floors ONCE over the whole tree. The required check context stays `test (3.13)` (the combiner's job key + matrix are unchanged); local parity is still the single-process `bash dev/test-cov.sh`. Shard balance is tuned by the committed `dev/.test_durations` — staleness only degrades balance, never correctness (pytest-split falls back to even chunking). Regenerate it from a full run when balance drifts: `uv run --group shard pytest --store-durations --durations-path dev/.test_durations` (the `shard` group is opt-in, out of the default `dev` group like `shuffle`).

### Test-Writing Doctrine (Non-Negotiable)

New tests — added in this repo or in any overlay repo — must lean **integration / E2E / functional**. Unit tests are reserved for pure logic that integration tests can't cover efficiently.

**Preferred patterns (in order):**

1. **Django test client** (`client.get(...)`, `client.post(...)`) for views and URL endpoints.
3. **`call_command("name", ...)`** for management commands — exercises the full Typer + Django glue.
4. **`subprocess.run(["t3", ...])`** (marked `@pytest.mark.integration`) when the bug would only surface through the real entry point.
5. **Real filesystem + real `git` under `tmp_path`** for anything that provisions worktrees, writes env files, or runs `git worktree add`. No mocking `Path`, `subprocess`, or git output. **Run those `git`/script subprocesses with a `GIT_*`-stripped env** (`{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}`): the suite can run from the inline pre-commit `pytest` hook, where the outer `git commit` exports `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` — inherited, they hijack the tmp-repo git calls so the test mutates the real repo. A test that passes standalone but fails under `git commit` is this. <!-- local-verification: cited-not-prescribed — names the hook's own pytest invocation, prescribes nothing -->
6. **Real Django ORM against the test DB** — use factories or `Model.objects.create(...)`, not mocked querysets.

**When a unit test is justified:**

- Pure logic with many branches that are painful to reach through a higher-level entry point (parsers, formatters, slug/branch-name builders, regex validators).
- Error paths that require deliberately malformed input (raising from the real caller is noisier than a direct call).
- Functions whose only effect is the return value (no I/O, no state, no side effects).

**Mock only unstoppable externals.** Network calls to GitHub / GitLab / Slack / Sentry, the clock (use `time_machine`), subprocess to third-party tools you don't own. **Don't** mock: teatree code, Django models, filesystem paths inside `tmp_path`, `git` (run real `git init` instead), or functions that happen to be annoying to set up — that last one is a sign the design is wrong, not a license to mock.

**Review gate:** new tests that are mostly `Mock()`, `patch()`, or assertions on `mock.call_args` are rejected unless the MR description explains why a higher-level test couldn't cover the same behavior. When converting existing mock-heavy tests, keep the coverage gate satisfied — rebalancing can't lower the number.

### Quality Gates

- **>=93% test coverage** — enforced by pytest-cov, `fail_under = 93`
  - `[tool.coverage.run] source` is `src/teatree` only — `hooks/scripts/*.py` (e.g. `hook_router.py`) is **outside** the project coverage gate. To verify 100% on changed hook lines, run a one-off measurement with an explicit rcfile (`coverage run --rcfile=<tmp.cfg> -m pytest <hook tests> -o addopts=`, with `[run] source = .` + `include = */hooks/scripts/<file>.py`), then `coverage report --include=…`. The standard `--cov` addopts won't measure it.
- **Ruff** — ALL rules enabled, specific ignores justified in pyproject.toml
- **ty** — static type checker with `error-on-warning = true`
- **tach** — enforces dependency boundaries
- **prek** (pre-commit) — runs all of the above on commit

### Key Conventions

- Python 3.13+ required. Use `X | Y` union syntax, not `Optional`.
- `from __future__ import annotations` is banned — use native syntax.
- No docstrings on classes/methods by policy (D1xx disabled). Self-documenting code.
- **Concise (directive #4).** Everything teatree authors — code comments, commit/PR bodies, reviews, Slack — is bullets, straight to the point, no prose. Comment only what the code cannot say (the *why*, a non-obvious constraint); never narrate the *what*. Be RIGHT but concise — brevity is an efficiency axis, not a licence to drop detail that changes the meaning.
- Management commands use `django-typer`, not `BaseCommand`.
- Git author: use whatever `git config user.name` / `git config user.email` is set to.
- Never add `Co-Authored-By` trailers to commits.

## Working on Skills

**Skills are in this repo.** When `/t3:retro` identifies a skill gap, improvements go directly into `skills/*/`.

After modifying skills: `t3 tool verify-gates` (commit AND push-stage hooks — a bare `prek run --all-files` skips the push-stage gates CI re-runs) then `bash dev/test-affected.sh` then commit.

## Abstraction Boundaries

- Teatree skills must never reference a specific project or overlay by name.
- Project-specific knowledge belongs in the generated host project's overlay app.
- User preferences belong in memory/config files, not skills.
- Use extension points or DB config settings for project context.

### teatree never reads assistant memory as a functional input (Non-Negotiable)

teatree resolves ALL required state from its own stores; an assistant's memory is never a source of truth for the factory (BLUEPRINT §17.1 invariant 14, [#3277](https://github.com/souliane/teatree/issues/3277)).

- Every piece of state teatree needs to *function* — config, gate enablement, publishing/merge doctrine, factory settings, loop state, trusted identities, credential routing — resolves from teatree's OWN stores: the DB-home `ConfigSetting` / `LoopState` tables (`teatree.config.cold_reader` / `cold_db`, or the ORM), `pass`, repo config. Never from `MEMORY.md`, `~/.claude/**/memory`, or a per-project memory file — those are per-assistant, per-machine, and not portable, so a memory dependency would make the factory behave differently on another machine / agent / fresh session.
- The only teatree paths that read the memory dir treat it as *product data teatree maintains or surfaces* (the `teatree.loops.dream` consolidation pass, the cold-tier recall injector's *advisory* context), each degrading to a no-op when the dir is absent. A memory-reading feature must NEVER gate a decision or resolve a required state value — even the recall injector's own `memory_recall_enabled` toggle reads from the DB, not from memory.
- The invariant is pinned by `tests/test_no_agent_memory_dependency.py` (identical runtime state with the memory dir absent vs. populated with contradicting bait). When adding any state resolver, read from teatree's own stores; do not introduce a new read of the assistant memory dir on a functional path.

## Things That Catch People

- The package is `teatree` (double-e) but the repo/CLI is `teatree`/`t3`.
- `DJANGO_SETTINGS_MODULE` is stripped from env when running `_managepy()` so the overlay's own settings win.
- **Running unit tests from another repo's working directory** (e.g., an overlay project) may fail with "No module named" errors because `DJANGO_SETTINGS_MODULE` from the outer shell leaks in before conftest can strip it. Fix: pass `--ds=tests.django_settings` to pytest, or `unset DJANGO_SETTINGS_MODULE` before invoking.
- Port allocation uses file-level locking (`teatree.utils.ports`) — never hardcode ports.
- The `t3 agent` command builds a system prompt from overlay detection + skill resolution, then `os.execvp`s into `claude`.
- Coverage omits only migrations. Everything else must be covered.
- The headless lane is an in-process session behind the `Harness` seam; whether a `claude` CLI child is spawned underneath is the backend's own `HarnessCapabilities.spawns_cli_child`. Interactive tasks exec the `claude` CLI directly in the operator's terminal.
- E2E tests use a separate settings module (`e2e.settings`) with file-based SQLite.
- **Submodule shadowing in `cli/__init__.py`.** When `cli/__init__.py` re-exports a name from a same-named submodule (`from teatree.cli.agent import agent`), the imported function overwrites the `cli.agent` submodule attribute on the parent package. Tests that do `import teatree.cli.agent as cli_agent_mod` then receive the function, not the module — `patch.object(cli_agent_mod, "os", ...)` fails with `does not have the attribute 'os'`. Use `import teatree.cli.agent as _agent` in `__init__.py` and reference attributes (`_agent.agent`) instead. The aliasing form does not bind to the parent package, so the submodule attribute survives intact.
