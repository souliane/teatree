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

## User-Facing Functions

| Function | Description |
|---|---|
| `t3_ticket <ticket> <description> <repo1> [...]` | Create ticket workspace with git worktrees |
| `t3_setup [variant]` | Full worktree setup: symlinks + env + DB + DSLR |
| `t3_db_refresh [variant]` | Refresh worktree DB (DSLR or full reimport) |
| `t3_finalize [msg]` | Squash worktree commits + rebase on default branch |
| `t3_clean` | Prune merged/gone worktrees and branches |
| `t3_backend` | Start backend (delegates to `wt_run_backend`) |
| `t3_frontend` | Start frontend (delegates to `wt_run_frontend`) |
| `t3_build_frontend` | Build frontend (delegates to `wt_build_frontend`) |
| `t3_tests` | Run tests (delegates to `wt_run_tests`) |
| `t3_restore_ci_db` | Restore DB from CI dump (delegates to `wt_restore_ci_db`) |
| `t3_reset_passwords` | Reset all user passwords (delegates to `wt_reset_passwords`) |
| `t3_trigger_e2e` | Trigger E2E tests on CI (delegates to `wt_trigger_e2e`) |
| `t3_quality_check` | Quality analysis — SonarQube, etc. (delegates to `wt_quality_check`) |
| `t3_fetch_ci_errors` | Fetch error logs from CI (delegates to `wt_fetch_ci_errors`) |
| `t3_fetch_failed_tests` | Extract failed test IDs from CI (delegates to `wt_fetch_failed_tests`) |
| `t3_start` | Orchestrate: detect variant → build → run frontend bg → run backend fg |

## Examples

### Example 1: New ticket with two repos

User says: "Set up ticket 1234 for my-backend and my-frontend"

1. `t3_ticket 1234 fix-address-fields my-backend my-frontend`
2. `cd $T3_WORKSPACE_DIR/<prefix>-my-backend-1234-fix-address-fields/my-backend && t3_setup`
3. `cd $T3_WORKSPACE_DIR/<prefix>-my-backend-1234-fix-address-fields/my-frontend && t3_setup`

Result: Two worktrees with isolated DBs, services, and env files ready.

### Example 2: Refresh a stale database

User says: "My DB is outdated, refresh it"

1. `t3_db_refresh my-variant`

Result: DSLR snapshot or fresh dump imported, migrations applied, superuser recreated.

### Example 3: Finalize and squash before review

User says: "Squash my commits and rebase"

1. `t3_finalize "feat(addresses): add postal code validation"`

Result: All worktree commits squashed into one, rebased on the default branch.
