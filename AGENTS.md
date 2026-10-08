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

What a gate may refuse at all is settled once, in `hooks/CLAUDE.md` § "Default-allow": developing teatree is allowed by default, four enumerated dangerous classes are not, and a gate that refuses ordinary development work is a defect to fix rather than a rule to obey.

## Repo Change Safety

- Never create a new plan file, memory file, journal file, or repo-instruction file in this repository without the user's explicit approval first.
- This includes files under paths such as `docs/plans/`, ad-hoc notes, repo-local memory artifacts, and new instruction/config files meant only for the agent.
- If a workflow or skill says to write such a file, stop and ask the user before doing it. Repo policy wins.

### Retiring/renaming a symbol: grep the FULL consumer set (Non-Negotiable)

Amplifies CLAUDE.md "No stale references". When a change retires or renames a function/field/registry-key/directive, the consumer set is **not just the handler/model that defines it**. Before claiming "no stale references / no dead code", grep across at minimum: (1) user-facing CLI help/docstrings and Typer `help=` strings; (2) generated docs (`docs/generated/*` — regenerate via the doc-generator/hook, never hand-edit); (3) PreCompact/snapshot/registry-serialization code that may read a now-unwritten field as a permanently-dead branch; (4) test fixtures that still write the retired shape (a green test guarding retired behavior is a stale consumer). A "retire X" PR/commit body asserting completeness without this grep is a false claim — it will bounce at cold review.

### Reused-ticket attestation: the gate-pass is NOT the guarantee (Non-Negotiable)

`t3 teatree workspace ticket <url>` is idempotent on `issue_url` (one ticket per issue) and does **not** clear the ticket's aggregated phase ledger — `Ticket.aggregate_phase_records()` unions `visited_phases` across ALL the ticket's sessions. A prior workstream's `testing/reviewing` therefore aggregates into the next workstream's view of the ledger; the structural guard is the `Ticket.reopen()` FSM transition (#1286), which retires every session's `visited_phases` and `phase_visits` so the next workstream re-earns its attestations from scratch. `reopen()` is the FSM-internal workstream-boundary call; the sanctioned `lifecycle clear-ledger --confirm` CLI is the operator-driven equivalent when a reuse path bypasses `reopen()` (e.g. an in-state reuse off PR_OPENED without an explicit reopen). Even with the ledger-retire, the coordinator integrity guarantee for multi-workstream / reused-ticket work stays the same defense-in-depth chain: **(a) coordinator-orchestrated independent cold review of THIS workstream's exact diff by a freshly-spawned reviewer sub-agent that has not seen the implementation (the spawn boundary is the independence, not a stored identity), (b) coordinator verification, (c) explicit per-workstream coordinator CLEAR to the review-loop** — recorded as the durable attestation receipt. Never chain attest→pr-create→merge on a pre-review gate-pass; STOP-for-review at pr-create and let the coordinator orchestrate review→CLEAR.

### GitHub access is HTTPS + `gh` credential helper, never SSH (Non-Negotiable)

Every GitHub remote is `https://github.com/<slug>.git`, and git delegates its authentication to `gh auth git-credential` — the helper `deploy/entrypoint.sh` installs with `gh auth setup-git`. The reason, in one line: auth is **token**-based through `gh`, so there is no key to distribute, mount or rotate. An SSH path is a second, undocumented credential that works on one box and not another, and the `gh` helper cannot serve it.

Three things are forbidden, because each one silently makes key material load-bearing:

```bash
git remote set-url origin git@github.com:owner/repo.git                  # github-ssh:allow doc example — FORBIDDEN
git config --global url."git@github.com:".insteadOf https://github.com/  # github-ssh:allow privacy-scan:allow doc example — FORBIDDEN
```

- an ssh-form GitHub remote — either spelling shown above, or an ssh Host alias resolving to github.com;
- a `url` … `insteadOf` rewrite pointing github.com at one, in any gitconfig scope;
- a mounted `~/.ssh` that becomes load-bearing for GitHub through either of the above.

When a git operation fails to authenticate, the answer is the **token**, never a key: `t3 push` resolves it, and `gh auth setup-git` rewires the helper. Two things stay explicitly untouched — the GitLab credential helper (a separate, legitimate path), and the remote *parsers* that accept ssh forms for arbitrary third-party repos (`utils/git_remote.py`, `core/public_identity.py`, `core/fleet/wire.py`, `entrypoint.sh`'s `gh_repo_slug`).

Two checks assert it: `tests/conformance/test_github_access_is_https.py` scans the tracked tree for a provisioning path that SETS one, and `t3 doctor`'s `_check_github_remotes_are_https` reads the live gitconfig of every discovered checkout — the untracked half no static scan can see. If SSH is ever genuinely wanted, it arrives as a deliberate planned change that edits both, never as an incidental config edit ([#4447](https://github.com/souliane/teatree/issues/4447)).

## Issue Creation (Non-Negotiable)

- **Never create issues without explicit user approval.** Always ask first — present the title and a summary, let the user decide.
- **Search the open backlog first, and reuse a host issue where one fits.** `gh issue list` returns 30 rows by default, so a first-page read reports "nothing open matches this" while the host sits at position 40 — pass `--limit 200`. Extending beats filing a near-duplicate: append the new evidence and acceptance criteria to the existing issue's **body**, so the host carries the whole ask. A cross-link from a second issue is not reuse.
- **The description is the specification; a comment is not.** A lane reads the issue body and nothing else, so a requirement, change request, scope change or decision posted as a comment is silently never executed. Record one with `t3 <overlay> ticket comment <url> --purpose requirement|change_request|scope_change|decision` (or the `<forge>_issue_note` MCP tool) — it appends a dated section to the description. Only `--purpose status|evidence` stays a comment, and a ticket sweep refuses even those. Both surfaces refuse a ticket the owner, the factory bot, or the CI workflows of a repo in the owner's own namespace (`github-actions[bot]`) did not file: external people's tickets stay theirs.
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

- **States:** not_started → scoped → work_started → plan_recorded → coded → tested → self_reviewed → pr_opened → review_requested → merged → retro_recorded → delivered (plus `review_delivered`, the reviewer-role terminal, and `ignored` for abandoned tickets)
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
- **Fields:** overlay, ticket (FK), visited_phases (JSONField), phase_visits (JSONField), started_at, ended_at, agent_id
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

### New model: the admin registers it (Non-Negotiable)

`core/admin.py` ends by registering every core model that has no hand-written
admin with `ReadOnlyAdmin`: list-only, 403 on add/change/delete/history and on
the per-row page, Text/JSON/Binary and secret-looking columns never selected,
filtered or searched. Adding a model therefore needs no admin edit. Customise
one with a `ReadOnlyAdmin` subclass ABOVE the call that ends `core/admin.py`;
an overlay customises a core model with `admin.site.unregister(Model)` then
`register`. A writable admin needs its label and reason in `WRITABLE_ADMINS`
in `tests/conformance/test_every_model_has_an_admin.py`, which judges an admin
by calling its permission methods. A model outside `core`, or one with a
composite primary key (Django refuses to register it; list it in
`UNREGISTRABLE` there), fails that test until handled.

### Squashing the core migrations (Non-Negotiable)

Whoever writes up or performs a squash of `src/teatree/core/migrations/`
follows both rules, in the code and in the squash PR's deploy note:

- **The squashed initial migration takes a NEW name.** Never `0001_initial`,
  and never a name any install already recorded, the current squash's own
  name included. Code from before the squash that meets a squashed database
  then finds none of its own rows and stops at its first `CreateModel` on an
  existing table. A reused name would instead read the squash as its own
  first migration and replay its later, destructive migrations over the
  squashed schema. Add every retired name to `_PRE_SQUASH_ROWS` in
  `tests/teatree_core/test_migration_squash_existing_db.py`, which refuses
  a squash that reuses one.
- **The squash has no automatic rollback.** The one-time rewrite of the
  `core` rows in `django_migrations` cannot be undone by `migrate`. The only
  rollback is restoring the pre-squash backup taken in the maintenance
  window, so the deploy note names that backup and says so.

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
3. Optionally implements `get_workspace_repos()` on the base, and the rest on the composed **facets**: `overlay.provisioning` (`env_extra`, `db_import_strategy`, `post_db_steps`, `symlinks`, `services_config`), `overlay.runtime` (`run_commands`, `test_command`), `overlay.e2e` (`env_extras`, `preflight`, `scenarios`), `overlay.review` (`can_auto_merge`, `visual_qa_targets`). Project metadata hooks (`validate_pr()`, `get_skill_metadata()`, `get_followup_repos()`, `get_ci_project_path()`, `get_e2e_config()`, `detect_variant()`) live on `OverlayMetadata` (`overlay.metadata`); credentials/URLs on `OverlayConfig` (`overlay.config`). A flat `get_*` on the base is the pre-facet shape — it no longer resolves, so an override written against it is dead code nothing calls
4. Registers via a `teatree.overlays` entry point in `pyproject.toml` (e.g., `my-overlay = "myapp.overlay:MyOverlay"`)
5. Gets auto-discovered by the overlay loader from `importlib.metadata.entry_points(group="teatree.overlays")`
6. Must be **import-safe before `django.setup()`**: entry points load at CLI-assembly time, so no module-level ORM (`teatree.core.models`) imports anywhere in the overlay module's import chain — annotate hooks with `teatree.overlay_sdk.WorktreeLike`/`TicketLike` (or an `if TYPE_CHECKING:` import) and defer runtime ORM access into function bodies (guarded by `tests/teatree_core/test_overlay_ep_import_pre_django.py`)

### Overlay API version (`teatree.__overlay_api_version__`)

Teatree exports `__overlay_api_version__` (a string) for overlays to assert against at import time. Bump it on every **breaking** change to the overlay-facing surface — `OverlayBase` method signatures, `Worktree`/`Ticket` fields overlays read, the entry-point contract, or runner protocols overlays may implement. Non-breaking additions (new optional hook, new helper) do not bump it.

**Pre-1.0 the counter is frozen at `"1"` and that rule is suspended.** Core and every registered overlay migrate in lockstep, so a bump would only manufacture a mismatch with nothing to detect — it would break each overlay's import-time pin against a core it is already compatible with. Ship the breaking change, migrate the overlays in the same wave, and leave the pin alone; `tests/test_package_smoke.py` fails a re-bump. The rule above takes effect from the first stable release.

Overlays should hard-fail at import (no shim, no deprecation warning) when the runtime teatree exposes a different version than what they were built against. CI catches the rest before merge.

## Backend Architecture

### API Protocols (`core/backend_protocols.py`)

Each external API concern is a `@runtime_checkable Protocol` in `teatree.core.backend_protocols`:

| Protocol | Purpose |
|---|---|
| `CodeHostBackend` | PR/MR creation, list own/review-requested PRs, PR comments, issue fetch + comments, file upload |
| `CIService` | Pipeline cancel, errors, failed tests, trigger, quality check |
| `MessagingBackend` | Mentions, DMs, posts, replies, reactions, user-id resolution |

Backends are auto-configured from the overlay's **config facet**. For example, `overlay.config.get_gitlab_token()` drives the GitLab backend (its URL is a pydantic field on `OverlayConfig`, not a method — there is no `get_gitlab_url()`); `overlay.config.get_slack_token()` and `get_review_channel()` drive Slack. No individual `TEATREE_*` Django settings are needed — each overlay carries its own configuration.

### Overlay Methods That Wrap Platform APIs Belong on a Backend

Overlay extension points are for **project-shaped** values: which repos exist, how to provision a worktree, which CI project to query, what tenants are valid. They are **not** for platform-API wrappers (HTTP, `gh`/`glab` shellouts, SDK clients). When an overlay method's body contains an HTTP call, a `subprocess` call to a platform CLI, or an SDK client instantiation, that's a sign the logic should move to (or delegate to) a core backend protocol.

Concretely:

- **Overlay** answers "which GitLab project is this overlay's CI?" (returns a string) — that's overlay-shaped.
- **Backend** answers "fetch the title of this issue" or "list my open MRs" (calls the GitLab API) — that's backend-shaped, and a protocol method like `CodeHostBackend.get_issue` or `CodeHostBackend.list_my_prs` already exists.

When adding a new overlay method, ask: would two overlays end up implementing this against the same API? If yes, write it once on the backend protocol and have overlays consume it. The periodic holistic review (`architectural-review` § 1) enforces this rule during audits, via the catalog entry `plugin-wraps-platform-api`.

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
- **Session resume:** `Task.session_continuation` decides which conversation a dispatch continues — `parent` (a `needs_user_input` answer), `self` (a row reopened in place) or `fresh` (everything else, including same-phase children). `claude_sdk` passes that conversation's session id as the SDK-native `resume=` option; `pydantic_ai` rehydrates its message history from `src/teatree/agents/pydantic_ai_resume.py`, where every finished run keeps its thread until nothing will continue it.

### Skill Loading

Skills in `skills/*/` are loaded via the plugin system (see `hooks/hooks.json`) or installed as symlinks into agent skill directories. Skills with "Auto-loaded as a dependency" descriptions are not user-invocable — loaded via `requires:` in other skills' frontmatter.

## Statusline

The persistent UI surface is a multi-line statusline rendered by `t3 loop tick`. Each zone (action_needed, in_flight, info) gets a distinct color. PR URLs render as terminal hyperlinks (OSC 8). `t3 loop status` shows the last-rendered output.

## Development Workflow

### Running

```bash
t3 --help                           # CLI help
t3 acme agent                       # Launch the configured agent with overlay context
t3 agent                            # Launch the configured agent (teatree-self development)
```

### Testing

```bash
bash dev/test-affected.sh           # the diff-scoped lane — the local default (`--full` opts into the whole suite)
bash dev/ci-parity-fast.sh          # pre-push inner loop: scoped prek + makemigrations + affected tests + push gate
bash dev/test-cov.sh                # Coverage lane: parallel + --cov --doctest-modules, 93% floor (CI parity)
prek run --all-files                # Commit-stage hooks ONLY (ruff, codespell, tach, ty)
t3 tool verify-gates                # FULL CI-parity gate set: commit, push and manual CI-job stages; prints the SHA it measured
t3 tool verify-gates --expect-sha <head>   # ...and refuse any tree that is not that head
bash dev/test-fast.sh               # Declared exception: whole host suite, Python 3.13, parallel
bash dev/test-matrix.sh             # Declared exception: Docker matrix, Python 3.13 + 3.14
```

**The local default is diff-scoped; the whole suite is a declared exception ([#3994](https://github.com/souliane/teatree/issues/3994)).** A local whole-tree sweep pays a second time for the run CI's required sharded lane is about to make, and it is on the critical path — measured as most of a 3.5h ticket cycle against a 30m target. `dev/test-affected.sh` is fail-safe TO FULL (a migration, a conftest / `factories.py` / test-settings edit, an unclassifiable path, a missing merge-base, or a change to the selection machinery itself each run everything on their own), so scoping never under-runs. Reach for `--full` / `dev/test-fast.sh` / `dev/ci-parity.sh` when a change is genuinely cross-cutting and declare it; CI's sharded `test (3.13)` lane and the 93% whole-tree floor remain the merge authority either way. `teatree.quality.local_verification` pins that every per-phase mandate surface names the scoped lane.

**The push path does NOT run the test suite — push -> CI is the gate.** The full suite is CI's job, never the local push path: a host under load times out unrelated wall-clock and concurrency tests and blocks an otherwise-good push (#112/#21/#38). The pre-push hooks are fast, scoped gates only (public-repo leak refusal, doc-update, comment-density, ensure-pr). Guarded by `tests/test_no_full_suite_on_pre_push.py` so a full-suite hook can't silently regress back onto pre-push.

**`dev/test-fast.sh` and `dev/test-matrix.sh` are explicit opt-in local runners, not the push path.** Run `dev/test-fast.sh` when you want the host suite locally on Python 3.13. Run `dev/test-matrix.sh` before merges that touch `dev/Dockerfile.test`, `uv.lock`, or system dependencies: it runs the suite in Docker across Python 3.13 + 3.14 and catches missing system dependencies and Python-version-specific differences the host gate can't. If the Dockerfile changed, remove the cached image first: `docker rmi teatree-test`.

**The CI `test (3.13)` gate is sharded 12-way behind an unchanged combiner.** The heavy lane is a `test-shard` matrix (`pytest-split --splits 12 --group N`, `-n auto` within each shard) that measures coverage but enforces NO floor; the `test` COMBINER job aggregates them — it fails if any shard failed, asserts the shards partition the suite exactly once (`scripts/ci/check_shard_completeness.py`), then combines the coverage and enforces the 93% floor + per-module floors ONCE over the whole tree. The required check context stays `test (3.13)` (the combiner's job key + matrix are unchanged); local parity is still the single-process `bash dev/test-cov.sh`. Shard balance is tuned by the committed `dev/.test_durations` — staleness only degrades balance, never correctness (pytest-split falls back to even chunking). Regenerate it from a full run when balance drifts: `uv run --group shard pytest --store-durations --durations-path dev/.test_durations` (the `shard` group is opt-in, out of the default `dev` group like `shuffle`). The weekly (Sunday) scheduled run does this for you onto the stable `ci/test-durations-refresh` branch. Fixes that refresh's own review requires (raised timeout ceilings, markers) belong **on that branch**: the next run merges the base under them and preserves every path other than `dev/.test_durations`, or refuses and says why — it no longer rebuilds the ref over them (#4717).

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

After modifying skills: `t3 tool verify-gates` (commit, push-stage and manual CI-job hooks — a bare `prek run --all-files` skips the push-stage and manual gates CI re-runs) then `bash dev/test-affected.sh` then commit. Report the SHA it says it measured alongside its exit code — the command takes no target, so the exit code alone does not say which tree earned it.

## Abstraction Boundaries

- Teatree skills must never reference a specific project or overlay by name.
- Project-specific knowledge belongs in the generated host project's overlay app.
- User preferences belong in memory/config files, not skills.
- Use extension points or DB config settings for project context.

### teatree never reads assistant memory as a functional input (Non-Negotiable)

teatree resolves ALL required state from its own stores; an assistant's memory is never a source of truth for the factory (BLUEPRINT §17.1 invariant 14, [#3277](https://github.com/souliane/teatree/issues/3277)).

- Every piece of state teatree needs to *function* — config, gate enablement, publishing/merge doctrine, factory settings, loop state, trusted identities, credential routing — resolves from teatree's OWN stores: the DB-home `ConfigSetting` / `LoopState` tables (`teatree.config.cold_reader` / `cold_db`, or the ORM), `pass`, repo config. Never from `MEMORY.md`, `~/.claude/**/memory`, or a per-project memory file — those are per-assistant, per-machine, and not portable, so a memory dependency would make the factory behave differently on another machine / agent / fresh session.
- The only teatree paths that read the memory dir treat it as *product data teatree maintains or surfaces* (the `teatree.loops.dream` consolidation pass), degrading to a no-op when the dir is absent. A memory-reading feature must NEVER gate a decision or resolve a required state value.
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

### Local stacks: stop, down, and teardown are three different things

Three tiers, never interchangeable — each keeps strictly less than the one above it.

| Tier | What survives | Who fires it |
|---|---|---|
| `docker compose stop` | the containers, and every volume attached to them | a human, or a repo's own test harness |
| `Worktree.stop_services` | the database, the checkout and `extra` — a later `start_services` is a fast resume, not a re-provision | the idle-stack reaper and the `max_concurrent_local_stacks` gate, automatically |
| `t3 <overlay> worktree teardown` | nothing — a `--volumes` compose down (`src/teatree/core/cleanup/cleanup.py:582`), the database dropped, `git worktree remove`, branch delete, then the row itself | you, explicitly |

Tier 2 has **no CLI leaf**: the `worktree` group's leaves run `provision`, `start`, `verify`, `ready`, `teardown`, and on through `status`, `diagnose` and the occupancy verbs — there is no `stop` between `start` and `teardown`, and none anywhere in the group (`src/teatree/cli/django_groups.py:82-93`), because `stop_services` is fired only by `teatree.loop.scanners.idle_stack_reaper` and `teatree.core.gates.local_stack_gate`. An agent that wants the reversible tier, finds no command for it, and settles for `teardown` has just dropped a database and removed a checkout.

Tier 2 stops the `<checkout>-test` sibling beside the worktree too — with `stop`, never `down`, because that sibling's test database is an anonymous volume a `down` would take with the containers (`_quiet_test_sibling`, `src/teatree/core/worktree/worktree_tasks.py:222-234`). Its failure never changes the demotion's verdict.

`down` is not one behaviour either. It removes containers and networks but **not** anonymous volumes, so its cost depends on where the stack keeps its data. A stack whose database lives on a server teatree provisioned survives it. A stack whose database is an anonymous volume — the shape you get when a service declares no volume and its image carries a `VOLUME` line — loses it: the volume is left dangling with nothing attached, and the next run rebuilds the database from empty. On a repo with a large migration history that is a long, silent tax. Establish which shape you are looking at before typing `down`.

**A running container is never a compose-project reap candidate.** Both compose-project reaping flavours — the `clean-all` deep clean and the automatic stale sweep — share one seam, `teatree.docker.reap._reapable_candidates` (`src/teatree/docker/reap.py:404-430`), whose last line is `sorted(mine - running)`. `_LIVE_CONTAINER_STATES` is `running`, `restarting`, `paused` (`reap.py:79`), and a project holding one such container is skipped whatever its age — a container that never stopped carries a days-old `StartedAt` and the docker zero `FinishedAt`, so an age test alone would read a live stack as abandoned. The consequence for a test harness that leaves its database and cache containers up after every run is that its stack is never reapable and accumulates until somebody stops it by hand. A docker that cannot answer reaps nothing: `running_compose_projects` returns `None` rather than an empty set, and the seam bails on it (`reap.py:244-253`, `:419-422`).

**Three routes admit a project, and only one of them proves itself.** They are one boolean at `reap.py:426-428`.

1. **The name proves itself.** `is_worktree_compose_project` matches `-wt<digits>$` (`reap.py:75`, `:256-272`) — the `<repo_path>-wt<ticket pk>` scheme teatree mints for its own stacks. Being a name test rather than a label test, it also covers stacks provisioned before the gate existed, which an ownership label could not.
2. **A registered row vouches for it.** Every OTHER name in the caller's `OwnedStacks.project_names` — the `<checkout-dir>-test` sibling a repo's own harness brings up beside the worktree — is a name compose derived from a directory BASENAME, so any two directories called the same thing share the project and the name proves nothing. It is admitted only once EVERY container's `working_dir` label is a checkout the caller owns (`_admitted_by_path`, `:316-330`).
3. **Path evidence, with no row at all.** `_is_orphaned_test_stack` (`:380-401`) reaches the case the other two structurally cannot: both derive ownership from a live `Worktree` row, and the defining property of a leaked stack is that its row is gone. **Four** proofs, each of which alone refuses the deploy stack: the `-test` suffix compose mints for a test sibling; every container having run from ONE checkout lying under a root teatree provisions into; the project being the name THAT directory mints, so a project merely passing through an owned checkout cannot borrow its ownership; and every entry of the `config_files` label being the repo's own `docker-compose.test.yml`. The single-`working_dir` requirement (`:393`) is one half of the second proof, not a fifth one — on its own it disqualifies nothing.

Route 3's roots come from `scanned_worktree_roots(canonical_worktree_root())` (`src/teatree/core/management/commands/_workspace/docker.py:69`) — configuration and registry rather than rows: the canonical worktree root, the caller's resolved workspace dir, and the PARENT directory of every registered worktree's checkout (`src/teatree/core/worktree/worktree_roots.py:113-129`). Registering one worktree in a directory therefore makes that whole directory a root, which is how a leaked `-test` stack from an unregistered sibling checkout inside it becomes reclaimable.

What is still invisible is narrower than "anything without a row", and the shape to get right is that route 3's single-`working_dir` requirement is NOT the general rule. Route 2 is an independent `or` branch testing **foreignness, not count**: `_admitted_by_path` admits on `working_dirs - checkout_paths` being EMPTY, so a basename-collided `-test` stack spanning two checkouts the registry BOTH owns is admitted and reaped. A count of two is not protection. What is genuinely out of reach: a stack one of whose `working_dir`s belongs to no owned checkout (that foreign path withholds the whole project under route 2, and its plurality defeats route 3), one running from a checkout under no scanned root whose project no row names, and anything at all with a container still up. A container carrying no compose-project label, from a hand-run `docker run`, is not even enumerated: every `docker ps` here filters on `label=com.docker.compose.project`.

The **idle-stack reaper is the deliberate exception**, and conflating it with the above is the mistake to avoid: it exists precisely to stop a stack whose containers ARE running. It does not reap the compose project — it demotes the `Worktree` row through `stop_services`, reversibly — and it proves QUIET rather than stopped, over nine keep-reasons applied cheapest-first (`preserve_reason`, `src/teatree/core/gates/idle_stack.py:284-308`): five structural (`:199-211` — not in a running state, never started, used inside the idle window, a live session or active task on the ticket, the process's own worktree), three active-delivery (`:214-222` — an external-delivery lease, a recent E2E run, an explicit pin), and last, because it shells out, a docker probe asking the containers themselves whether they emitted anything in the window (`:267-281`). That probe is the only guard that can see an out-of-band driver — a Playwright run, a browser or a `curl` writes no row any control-plane guard can read — and the only one whose "cannot tell" answer is honest rather than inferred. `UNKNOWN` KEEPS: a stack the reaper cannot prove idle is not idle.

### Every `t3` invocation is a Django boot

An overlay-scoped or management command does not run in the CLI's own process. `teatree.cli.overlay.managepy` (`src/teatree/cli/overlay.py:125-137`) spawns a fresh subprocess either way: its primary branch runs the PROJECT's own `manage.py`, when the project has one and its env is drivable (`:132-134`), and `python -m teatree` with `DJANGO_SETTINGS_MODULE` set is the fallback (`:136-137`). Both are full Django boots, so each call pays a full Django startup — the CLI carries 112 bare `ensure_django()` call sites under `src/teatree/cli/`. On a deployed box `deploy/t3` wraps that again, `exec`ing into the already-running `teatree-worker` (`deploy/t3:23` makes it the default service; the exec itself is `:1127`) and falling back to a one-off `run --rm` worker when the stack is down (`:1183`).

The floor is seconds, not milliseconds. Re-measured 2026-09-07 on one box: `t3 --help`, the cheapest call there is, took 4.04 s, 4.33 s and 4.82 s over three consecutive runs; two earlier sessions on the same box spanned 3.3 s to 5.9 s. It moves with host load, so carry the magnitude and not the number.

That makes `t3` the right tool for anything needing teatree's state and an expensive one for anything else. Two consequences:

- **Do not poll `t3 --help` to discover a command.** Use the MCP `command_search(query)` read instead: it returns each matching leaf's full path, its help summary, and whether it emits `--json`, and its own registration says to reach for it FIRST when unsure which command exists.
- **A `t3` call on a fixed cadence pays that boot on every tick.** Under a `cpus` cap the polling itself can consume the allowance. Weigh the boot before putting one inside a loop.
