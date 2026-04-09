# TeaTree — Agent Instructions

This is the teatree repo — both the Python package (`src/teatree/`) and the workflow skills (`skills/*/`). You are developing teatree itself, not using it on a downstream project.

## Repo Change Safety

- Never create a new plan file, memory file, journal file, or repo-instruction file in this repository without the user's explicit approval first.
- This includes files under paths such as `docs/plans/`, ad-hoc notes, repo-local memory artifacts, and new instruction/config files meant only for the agent.
- If a workflow or skill says to write such a file, stop and ask the user before doing it. Repo policy wins.

## Issue Creation (Non-Negotiable)

- **Never create issues without explicit user approval.** Always ask first — present the title and a summary, let the user decide.
- **Teatree is a public repository.** Only generic, project-agnostic issues belong here. Never mention downstream project names, tenant names, customer names, internal architecture, feature flags, or any proprietary information.
- **Overlay-specific issues go on the overlay repository.** If an issue involves both core teatree and an overlay, create it on the overlay repo and reference the core component — not the other way around.
- **When in doubt, ask.** If you're unsure whether an issue is generic or overlay-specific, ask the user before creating it anywhere.
- **Link commits to issues.** When fixing a tracked issue, use `Fixes #<number>` or `Closes #<number>` in the commit message body (not the first line) to auto-close it on merge. Use `Relates-to #<number>` for partial progress.

## What TeaTree Is

A multi-repo worktree lifecycle manager for AI-assisted development. Target: service-oriented projects with databases and CI pipelines (any language). Not for docs-only repos or CLI tools.

It provides:

- A unified CLI (`uv run t3`) for worktree creation, provisioning, dev servers, CI, and delivery
- A Django app (`teatree.core`) with 5 models driven by `django-fsm` state machines
- An overlay system for downstream project customization (`OverlayBase`)
- Backend protocols for pluggable external integrations
- Agent workflow skills (`skills/*/`) for the full development lifecycle
- A dashboard (django-htmx) for monitoring tickets, tasks, and agent sessions

## Repo Layout

```
src/teatree/           Python package (the Django app + CLI)
  cli/                 Typer CLI package — the `t3` entry point
  config.py            ~/.teatree.toml parsing, overlay discovery
  skill_loading.py     Skill selection policy (phase → skills, companion resolution)
  skill_deps.py        Transitive dependency and companion resolution
  core/                Django app: models, managers, views, selectors, management commands
    models/            Ticket, Worktree, Session, Task, TaskAttempt (FSM states)
    selectors/         Selector functions for dashboard views (no domain logic in views)
    overlay.py         OverlayBase ABC — extension point for downstream projects
    overlay_loader.py  Loads the active overlay class from Django settings
    management/commands/  Django-typer commands (lifecycle, workspace, db, run, followup, pr, tasks)
    views/             Dashboard views (dashboard.py, launch.py, actions.py)
    templates/         HTMX-driven dashboard templates
  backends/            Pluggable service integrations
    protocols.py       Protocol classes (CodeHost, CIService, IssueTracker, ChatNotifier, ErrorTracker)
    loader.py          Settings-driven backend loader (import_string, lru_cache)
    gitlab.py          GitLab API client
    gitlab_ci.py       GitLab CI pipeline operations
    slack.py, notion.py, sentry.py  Other integrations
  agents/              Agent runtime
    headless.py        SDK tasks via `claude -p` (capture JSON output)
    web_terminal.py    Interactive tasks via ttyd (browser-based terminal)
    sdk.py             SDK runtime adapter (EchoRuntime registry)
    terminal.py        Interactive runtime adapter
    services.py        Runtime registry, settings readers
    skill_bundle.py    Skill dependency resolver for agent launch
    prompt.py          System context and task prompt builders
    result_schema.py   JSON schema for structured agent output
  utils/               Git helpers, port allocation, subprocess wrappers
  overlay_init/        `t3 startoverlay` templates (overlay package + app)
skills/*/              Workflow skills (SKILL.md + references/)
tests/                 Pytest suite (>90% coverage required)
e2e/                   Playwright E2E tests for dashboard
scripts/               Standalone Python CLI scripts
hooks/                 Agent platform hooks (Claude Code hook_router, statusline, etc.)
```

## 5 Models

### Ticket — Core delivery entity

- **States:** not_started → scoped → started → coded → tested → reviewed → shipped → in_review → merged → delivered
- **Fields:** issue_url, variant, repos (JSONField), state, extra (JSONField)
- **Key methods:** scope(), start(), code(), test(), review(), ship(), rework()

### Worktree — Per-repo lifecycle (FK → Ticket)

- **States:** created → provisioned → services_up → ready
- **Fields:** ticket (FK), repo_path, branch, state, ports (JSONField), db_name
- **Key methods:** provision(), start_services(), verify(), db_refresh(), teardown()

### Session — Quality gate tracker (FK → Ticket)

- Tracks visited phases across tasks within a conversation
- **Fields:** ticket (FK), agent_id, started_at, ended_at
- Quality gates enforce ordering: reviewing requires testing, shipping requires reviewing

### Task — Agent work unit (FK → Ticket, Session)

- **Fields:** ticket (FK), session (FK), execution_target (headless/interactive), execution_reason, status (pending/claimed/completed/failed), phase
- **Claim/lease:** claimed_at, claimed_by, lease_expires_at, heartbeat_at
- **Key methods:** claim(), route_to_headless(), route_to_interactive(), complete(), fail()

### TaskAttempt — Execution history (FK → Task)

- **Fields:** task (FK), execution_target, ended_at, exit_code, error, result (JSONField), launch_url
- Enables cross-task failure querying and audit trail

## Three-Tier Command Split

| Tier | Tool | Examples | Needs Django? |
|------|------|----------|---------------|
| Runtime commands | Django management commands (django-typer) | `lifecycle setup`, `tasks work-next-sdk`, `followup refresh` | Yes |
| Bootstrap commands | `t3` Typer CLI | `t3 startoverlay`, `t3 agent`, `t3 overlays` | No |
| Internal utilities | Python modules in `utils/` | Port allocation, git helpers, DB ops | Imported by commands |

## Overlay System

An overlay is a lightweight Python package that customizes teatree. It:

1. Subclasses `OverlayBase` (from `teatree.core.overlay`)
2. Implements mandatory hooks: `get_repos()`, `get_provision_steps(worktree)`
3. Optionally implements: `get_env_extra()`, `get_run_commands()`, `get_db_import_strategy()`, `get_post_db_steps()`, `get_symlinks()`, `get_services_config()`, `validate_mr()`, `get_skill_metadata()`, `get_followup_repos()`, `get_ci_project_path()`, `get_e2e_config()`, `detect_variant()`, `get_workspace_repos()`
4. Registers via a `teatree.overlays` entry point in `pyproject.toml` (e.g., `my-overlay = "myapp.overlay:MyOverlay"`)
5. Gets auto-discovered by the overlay loader from `importlib.metadata.entry_points(group="teatree.overlays")`

## Backend Protocols

Each external concern is a `Protocol` in `teatree.backends.protocols`:

| Protocol | Purpose |
|---|---|
| `CodeHost` | PR/MR creation, list open PRs |
| `CIService` | Pipeline cancel, errors, failed tests, trigger, quality check |
| `IssueTracker` | Issue fetching |
| `ChatNotifier` | Team notifications |
| `ErrorTracker` | Sentry-like error tracking |

Backends are auto-configured from overlay methods. For example, `get_gitlab_token()` and `get_gitlab_url()` on the overlay class drive the GitLab backend; `get_slack_token()` and `get_review_channel()` drive Slack. No individual `TEATREE_*` Django settings are needed -- each overlay carries its own configuration.

## Runtime Abstraction

```python
TEATREE_HEADLESS_RUNTIME = "claude-code"     # Runtime for headless tasks
TEATREE_INTERACTIVE_RUNTIME = "codex"        # Runtime for interactive tasks
TEATREE_TERMINAL_MODE = "same-terminal"      # Terminal strategy
TEATREE_HEADLESS_USE_CLI = True              # Use `claude -p` instead of Anthropic API
```

## Agent Runtime

### Interactive Sessions (web_terminal.py)

Interactive tasks launch via ttyd — a web-based terminal that wraps `claude`. This is the only interactive mode.

- ttyd must be installed (`brew install ttyd`)
- ttyd must be spawned with `--writable` (read-only otherwise)
- Dashboard Launch button → POST `/tasks/<id>/launch/` → returns `{"launch_url": "..."}` → JS opens in new tab
- Command: `claude --append-system-prompt <context>` (no `-p`, interactive mode)
- **Session resume:** When a parent headless task carried a `session_id` (via `Session.agent_id`), the interactive session resumes it with `claude --resume <session_id>` — preserving full context from the headless run.

### Headless Sessions (headless.py)

SDK tasks run `claude -p <prompt> --append-system-prompt <context> --output-format json`.

- When `TEATREE_SDK_USE_CLI = True`, always uses `claude` binary (no API key needed)
- Parses JSON result from stdout, validates against `result_schema.py`
- If result contains `needs_user_input: true`, reroutes task to user_input queue
- Stores result in `TaskAttempt.result`
- **Session resume:** When a `parent_task` chain contains a previous `agent_session_id` (from a prior headless or interactive run), headless prepends `--resume <session_id>` to continue with full context.

### Skill Loading

Skills in `skills/*/` are loaded via the plugin system (see `hooks/hooks.json`) or installed as symlinks into agent skill directories. Skills with "Auto-loaded as a dependency" descriptions are not user-invocable — loaded via `requires:` in other skills' frontmatter.

## Dashboard

Selector-backed views with django-htmx. No domain logic in views.

**Panels** (auto-refresh via HTMX):

- Runtime Summary — counter cards (in-flight tickets, active worktrees, pending SDK/user-input tasks)
- In-Flight Tickets — table with MR data, pipeline status, approvals, Auto/Interactive task creation buttons
- SDK Task Queue — pending/claimed SDK tasks with Execute/Cancel buttons, result summaries
- User-Input Queue — pending/claimed interactive tasks with Launch/Cancel buttons
- Active Sessions — running Claude processes on this machine

**CSS conventions:** Use `.pill` / `.pill-btn` classes defined in `dashboard.html <style>` for all badges and buttons. These enforce `whitespace-nowrap` globally. Sizes: `pill pill-sm` (standard badges), `pill pill-xs` (small badges), `pill-btn pill-xs` (action buttons). Only add color/border as Tailwind utilities alongside the CSS class — never inline the full utility pattern.

**Endpoints:**

- `GET /` — full dashboard
- `GET /dashboard/panels/<panel>/` — HTMX panel refresh (requires HX-Request header)
- `POST /dashboard/sync/` — trigger followup sync
- `POST /tasks/<id>/launch/` — claim and execute (SDK) or launch ttyd (interactive)
- `POST /tasks/<id>/cancel/` — cancel task (sets to FAILED)
- `POST /tickets/<id>/create-task/` — create SDK or user_input task

## Development Workflow

### Running

```bash
uv run t3 --help                    # CLI help
uv run t3 acme dashboard            # Start overlay dashboard (auto-finds free port)
uv run t3 acme agent                # Launch Claude Code with overlay context
uv run t3 agent                     # Launch Claude Code (teatree-self development)
```

### Testing

```bash
uv run pytest                       # Unit tests with coverage (>90% required)
uv run pytest e2e/ -x               # E2E tests with Playwright
prek run --all-files                 # Pre-commit hooks (ruff, codespell, tach, ty)
bash dev/test-matrix.sh             # Docker matrix: Python 3.13 + 3.14 (MANDATORY before push)
```

**Always run `dev/test-matrix.sh` before claiming a fix works.** Local `uv run pytest` only tests one Python version with locally-installed tools. The Docker matrix catches missing system dependencies and Python-version-specific coverage differences. If the Dockerfile changed, remove the cached image first: `docker rmi teatree-test`.

### Quality Gates

- **>90% test coverage** — enforced by pytest-cov, `fail_under = 93`
- **Ruff** — ALL rules enabled, specific ignores justified in pyproject.toml
- **ty** — static type checker with `error-on-warning = true`
- **tach** — enforces dependency boundaries
- **prek** (pre-commit) — runs all of the above on commit

### Key Conventions

- Python 3.13+ required. Use `X | Y` union syntax, not `Optional`.
- `from __future__ import annotations` is banned — use native syntax.
- No docstrings on classes/methods by policy (D1xx disabled). Self-documenting code.
- Management commands use `django-typer`, not `BaseCommand`.
- Git author: use whatever `git config user.name` / `git config user.email` is set to.
- Never add `Co-Authored-By` trailers to commits.

## Working on Skills

**Skills are in this repo.** When `/t3:retro` identifies a skill gap, improvements go directly into `skills/*/`.

After modifying skills: `prek run --all-files` then `uv run pytest` then commit.

## Abstraction Boundaries

- Teatree skills must never reference a specific project or overlay by name.
- Project-specific knowledge belongs in the generated host project's overlay app.
- User preferences belong in memory/config files, not skills.
- Use extension points or `~/.teatree.toml` variables for project context.

## Dashboard Smoke-Test Checklist

After any dashboard fix, verify the full flow before declaring done:

1. Dashboard loads (HTTP 200)
2. Headless tasks launch without error (check task status after worker processes)
3. Interactive tasks launch without error (check ttyd)
4. Errors display in the UI (check queue panels for red banners)
5. Concurrent launches don't cause SQLite locking

## Things That Catch People

- The package is `teatree` (double-e) but the repo/CLI is `teatree`/`t3`.
- `DJANGO_SETTINGS_MODULE` is stripped from env when running `_managepy()` so the overlay's own settings win.
- **Running unit tests from another repo's working directory** (e.g., an overlay project) may fail with "No module named" errors because `DJANGO_SETTINGS_MODULE` from the outer shell leaks in before conftest can strip it. Fix: pass `--ds=tests.django_settings` to pytest, or `unset DJANGO_SETTINGS_MODULE` before invoking.
- Port allocation uses file-level locking (`teatree.utils.ports`) — never hardcode ports.
- The `t3 agent` command builds a system prompt from overlay detection + skill resolution, then `os.execvp`s into `claude`.
- Coverage omits only migrations. Everything else must be covered.
- ttyd without `--writable` = read-only terminal = claude can't work.
- `claude -p` is headless (exits immediately). Interactive sessions use `claude` without `-p`.
- E2E tests use a separate settings module (`e2e.settings`) with file-based SQLite.
