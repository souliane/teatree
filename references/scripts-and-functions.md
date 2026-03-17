# Scripts and Functions

> Load when you need to find available shell functions, understand script internals, or see usage examples.

---

## Source the Bootstrap

```bash
source $T3_REPO/scripts/lib/bootstrap.sh
```

## Scripts

### Bash (shell integration — must eval in caller's shell)

- `scripts/lib/bootstrap.sh` — Thin wrappers that delegate to Python. Only file sourced by .zshrc.
- `scripts/lib/shell_helpers.sh` — `_detect_ticket_dir`, `_source_env_file`, `_direnv_eval`. Must eval in caller's shell.

### Python (core logic)

- `scripts/lib/init.py` — Load defaults + frameworks + project overrides (called once per invocation)
- `scripts/lib/registry.py` — Extension point registry: `register()`, `get()`, `call()`. 3-layer priority: default < framework < project
- `scripts/lib/extension_points.py` — Default no-op implementations for all extension points (registered at 'default' layer)
- `scripts/lib/git.py` — `default_branch(repo)`
- `scripts/lib/db.py` — `db_restore(name, dump)`, `db_exists(name)`
- `scripts/lib/env.py` — `detect_ticket_dir()`, `resolve_repo_dir()`, `find_free_ports()`, `WorktreeContext` dataclass, `resolve_context()`
- `scripts/lib/ports.py` — `port_in_use(port)`, `free_port(port)`
- `scripts/lib/dashboard_renderer.py` — `render_dashboard(data)` → HTML string. Pure renderer for followup dashboard

### Python (lifecycle commands)

- `scripts/ws_ticket.py` — `ws_ticket(ticket, desc, *repos)` CLI entry point
- `scripts/wt_setup.py` — `wt_setup(variant, url)` + `compute_compose_project_name()`. Writes `COMPOSE_PROJECT_NAME` to `.env.worktree`
- `scripts/wt_db_refresh.py` — `wt_db_refresh(variant)` CLI entry point
- `scripts/wt_finalize.py` — `wt_finalize(msg)` CLI entry point
- `scripts/git_clean_them_all.py` — `git_clean_them_all()` CLI entry point
- `scripts/generate_dashboard.py` — `generate_dashboard(input, output)` renders followup.html from followup.json

### Python (framework plugins)

- `scripts/frameworks/django.py` — Django overrides (auto-detected via manage.py, registered at 'framework' layer)

## Python Script Conventions

When adding a new `scripts/*.py` file, pre-commit hooks enforce:

1. **uv shebang:** `#!/usr/bin/env -S uv run --script`
2. **Inline script metadata:** `# /// script` block with `dependencies` list (even if empty)
3. **No raw `sys.argv`:** Use `typer` for CLI argument parsing — add `typer>=0.12` to inline deps
4. **Editorconfig:** 4-space indentation everywhere (including docstrings)
5. **Make executable:** `chmod +x` the script file
6. **Type annotations:** `ty-check` runs on all files — use `Path | None` not `Path` for optional typer args

Core logic goes in `scripts/lib/` modules (pure functions, testable). The `scripts/*.py` file is a thin CLI wrapper.

## COMPOSE_PROJECT_NAME per Worktree

`wt_setup.py` computes a unique `COMPOSE_PROJECT_NAME` from the ticket number:

```text
$T3_WORKSPACE_DIR/<prefix>-my-backend-1234-foo/my-backend → COMPOSE_PROJECT_NAME=my-backend-wt1234
```

Written to `.env.worktree` and picked up by `.envrc` via:

```bash
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-my-backend}"
```

Main repo keeps its default name. Worktrees get unique names → parallel containers.

## `t3` CLI (Unified Entry Point)

All operations go through the `t3` command. Commands are grouped by concern. The individual `t3_*` shell functions have been consolidated into nested CLI subcommands.

### `t3 lifecycle` — Lifecycle state machine

| Command | Description |
|---|---|
| `t3 lifecycle status [--json]` | Show current worktree state, ports, DB, and available transitions |
| `t3 lifecycle setup [VARIANT]` | Provision worktree: ports, env, symlinks, DB |
| `t3 lifecycle start` | Start dev servers (backend + frontend), then verify |
| `t3 lifecycle clean` | Teardown worktree — stop services, drop DB, clean state |
| `t3 lifecycle diagram` | Print the lifecycle state diagram as Mermaid |

### `t3 workspace` — Workspace management

| Command | Description |
|---|---|
| `t3 workspace ticket <NUM> <DESC> <REPO...>` | Create ticket workspace with git worktrees |
| `t3 workspace finalize [MSG]` | Squash worktree commits + rebase on default branch |
| `t3 workspace clean-all` | Prune merged/gone worktrees and branches across all repos |

### `t3 run` — Dev servers and test runners

| Command | Description |
|---|---|
| `t3 run backend` | Start backend dev server |
| `t3 run frontend` | Start frontend dev server |
| `t3 run build-frontend` | Build frontend app |
| `t3 run tests` | Run project tests |
| `t3 run verify` | Verify dev services respond via HTTP |

### `t3 ci` — CI pipeline interaction

| Command | Description |
|---|---|
| `t3 ci cancel` | Cancel stale CI pipelines |
| `t3 ci trigger-e2e` | Trigger E2E tests on CI |
| `t3 ci fetch-errors` | Fetch error logs from CI |
| `t3 ci fetch-failed-tests` | Extract failed test IDs from CI |
| `t3 ci quality-check-check` | Run quality analysis (SonarQube, etc.) |

### `t3 db` — Database operations

| Command | Description |
|---|---|
| `t3 db refresh` | Re-import database from dump/DSLR |
| `t3 db restore-ci` | Restore database from CI dump |
| `t3 db reset-passwords` | Reset all user passwords to a known value |

### `t3 mr` — Merge request and ticket workflow

| Command | Description |
|---|---|
| `t3 mr create` | Create merge request |
| `t3 mr check-gates` | Check transition gates for ticket status |
| `t3 mr fetch-issue` | Fetch issue context from tracker |
| `t3 mr detect-tenant` | Detect tenant variant |
| `t3 mr followup` | Collect followup dashboard data |

## Examples

### Example 1: New ticket with two repos

User says: "Set up ticket 1234 for my-backend and my-frontend"

1. `t3 workspace ticket 1234 fix-address-fields my-backend my-frontend`
2. `cd $T3_WORKSPACE_DIR/<prefix>-my-backend-1234-fix-address-fields/my-backend && t3 lifecycle setup`
3. `cd $T3_WORKSPACE_DIR/<prefix>-my-backend-1234-fix-address-fields/my-frontend && t3 lifecycle setup`

Result: Two worktrees with isolated DBs, services, and env files ready.

### Example 2: Refresh a stale database

User says: "My DB is outdated, refresh it"

1. `t3 db refresh`

Result: DSLR snapshot or fresh dump imported, migrations applied, superuser recreated.

### Example 3: Finalize and squash before review

User says: "Squash my commits and rebase"

1. `t3 workspace finalize "feat(addresses): add postal code validation"`

Result: All worktree commits squashed into one, rebased on the default branch.
