# Overlay API

The overlay is the integration point between teatree (generic) and your project (specific). You subclass `OverlayBase` and implement the hooks that teatree calls during worktree lifecycle operations.

## Setup

1. Create a Python package with an `OverlayBase` subclass:

   ```python
   # myapp/overlay.py
   from teatree.core.overlay import OverlayBase

   class MyOverlay(OverlayBase):
       def get_repos(self) -> list[str]:
           return ["backend", "frontend"]

       def get_provision_steps(self, worktree):
           return []
   ```

2. Register it as a `teatree.overlays` entry point in your `pyproject.toml`:

   ```toml
   [project.entry-points."teatree.overlays"]
   my-overlay = "myapp.overlay:MyOverlay"
   ```

3. Install your package alongside teatree (`pip install -e .` during development).

The overlay loader discovers all installed overlays from entry points at startup, instantiates each class once, and caches them. Teatree calls overlay methods from management commands when it needs project-specific information. If multiple overlays are installed, commands accept an overlay name to disambiguate.

## Configuration via `OverlayConfig`

Overlay-specific configuration (tokens, URLs, service credentials) lives on `OverlayConfig`, accessed as `overlay.config`. Configure via an `overlay_settings` module (Django-style) or via `[overlays.<name>]` in `~/.teatree.toml`.

### Static attributes

Set these as `UPPER_CASE` constants in a settings module, or as `lower_case` keys in TOML:

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `gitlab_url` | `"https://gitlab.com/api/v4"` | GitLab API base URL |
| `github_owner` | `""` | GitHub user or org that owns the project board |
| `github_project_number` | `0` | GitHub Projects v2 board number |
| `require_ticket` | `False` | Whether to enforce a tracked issue before coding/shipping |
| `known_variants` | `[]` | Tenant variant identifiers |
| `mr_auto_labels` | `[]` | Labels auto-applied to merge requests |
| `frontend_repos` | `[]` | Frontend repo names (for build steps) |
| `workspace_repos` | `[]` | Repo paths relative to `workspace_dir` (supports nested paths) |
| `protected_branches` | `[]` | Branch names that should never be deleted during cleanup |
| `dev_env_url` | `""` | Development environment base URL |
| `dashboard_logo` | `""` | Path or URL for the dashboard logo |

### Secret getters

Override these in a subclass, or use `*_PASS_KEY` settings to auto-register readers from the `pass` password store:

| Method | Default | Purpose |
|--------|---------|---------|
| `get_gitlab_token()` | `""` | GitLab API token |
| `get_gitlab_username()` | `""` | GitLab username for MR assignment |
| `get_github_token()` | `""` | GitHub API token |
| `get_slack_token()` | `""` | Slack bot token for notifications |
| `get_review_channel()` | `("", "")` | `(channel_name, channel_id)` for review notifications |

Each overlay carries its own `OverlayConfig`, so multi-overlay setups can point to different GitLab instances or Slack workspaces.

## Metadata via `OverlayMetadata`

Project metadata, CI integration, MR validation, and skill registration live on `OverlayMetadata`, accessed as `overlay.metadata`. Subclass and assign to `OverlayBase.metadata`:

| Method | Default | Purpose |
|--------|---------|---------|
| `validate_mr(title, description)` | no errors/warnings | Validate MR title and description against project conventions |
| `get_followup_repos()` | `[]` | Repos to check during follow-up sync |
| `get_skill_metadata()` | `{}` | Skill path, remote patterns, trigger index for the overlay's companion skills |
| `get_ci_project_path()` | `""` | CI project path for pipeline triggers and evidence posting |
| `get_e2e_config()` | `{}` | E2E test configuration (project path, settings module, test dir) |
| `detect_variant()` | `""` | Detect the current tenant variant from project context |
| `get_tool_commands()` | `[]` | Custom tool commands exposed via `t3 <overlay> tool run` |
| `get_issue_title(url)` | `""` | Fetch the title of an issue from its URL |

## `OverlayBase`

### Mandatory hooks

These are abstract -- you must implement them.

#### `get_repos() -> list[str]`

Return the list of repository names your project manages. Teatree uses this to know which repos to create worktrees in.

#### `get_provision_steps(worktree: Worktree) -> list[ProvisionStep]`

Return the ordered steps to provision a worktree after creation. Each step is a `ProvisionStep` with a name, callable, and optional description. Steps run sequentially during `worktree provision`.

### Provisioning hooks

These have default implementations that return empty/neutral values. Override them as needed.

#### `get_env_extra(worktree: Worktree) -> dict[str, str]`

Extra environment variables to set for a worktree. Defaults to `{}`.

#### `get_db_import_strategy(worktree: Worktree) -> DbImportStrategy | None`

How to import/restore a database for this worktree. Returns `None` if no DB import is needed.

#### `db_import(worktree, *, force, slow_import, dslr_snapshot, dump_path) -> bool`

Run the actual database import logic. Called by `worktree provision` and `db refresh`. Returns `True` on success. Defaults to `False` (no-op).

#### `get_post_db_steps(worktree: Worktree) -> list[ProvisionStep]`

Steps to run after a database import (migrations, data fixups). Defaults to `[]`.

#### `get_reset_passwords_command(worktree: Worktree) -> ProvisionStep | None`

Return a provision step that resets user passwords to a known dev value. Called after post-DB steps and by `db reset-passwords`. Defaults to `None`.

#### `get_envrc_lines(worktree: Worktree) -> list[str]`

Extra lines to append to the worktree's `.envrc` file. Defaults to `[]`.

#### `get_symlinks(worktree: Worktree) -> list[SymlinkSpec]`

Symlinks to create in the worktree (e.g., shared config files, node_modules). Defaults to `[]`.

#### `get_services_config(worktree: Worktree) -> dict[str, ServiceSpec]`

Service configuration (Docker compose files, readiness checks, shared vs. per-worktree). Defaults to `{}`.

#### `get_compose_file(worktree: Worktree) -> str`

Return the path to the docker-compose file for this worktree. Used by `worktree start` and `run backend`. Defaults to `""`.

### Run hooks

#### `get_run_commands(worktree: Worktree) -> RunCommands`

Commands to run services (backend, frontend, build-frontend, etc.) for a worktree. Returns a dict mapping service name to command args or `RunCommand`. Defaults to `{}`.

#### `get_pre_run_steps(worktree: Worktree, service: str) -> list[ProvisionStep]`

Steps to run before starting a specific service (e.g., copy customer config, refresh translations). Called for each service during `worktree start` and `run frontend`. Defaults to `[]`.

#### `get_test_command(worktree: Worktree) -> list[str] | RunCommand`

The command to run the project test suite. Used by `run tests`. Defaults to `[]`.

#### `get_verify_endpoints(worktree: Worktree) -> dict[str, str]`

Custom health-check URL paths per service. Keys match `worktree.ports` entries (e.g., `"backend"`, `"frontend"`). Values are URL paths (e.g., `"/admin/login/"`). Services not listed fall back to `/`. Defaults to `{}`.

#### `get_timeouts() -> dict[str, int]`

Overlay-specific timeout overrides in seconds. Keys match `teatree.timeouts` operation names (e.g., `"setup"`, `"db_import"`). `0` disables the timeout. Only return overrides — missing keys use core defaults. Defaults to `{}`.

#### `get_cleanup_steps(worktree: Worktree) -> list[ProvisionStep]`

Extra cleanup steps run before a worktree is removed (Docker containers, cache dirs, etc.). Defaults to `[]`.

#### `get_health_checks(worktree: Worktree) -> list[HealthCheck]`

Post-provision health checks to verify the worktree is functional. The default checks verify: worktree path exists, symlinks are valid, DB name is set. Override to add project-specific checks.

#### `get_workspace_repos() -> list[str]`

Repo paths relative to `workspace_dir`. Supports nested paths (e.g., `souliane/teatree`). Reads from `config.workspace_repos` first; falls back to `get_repos()`. Defaults to `get_repos()`.

## Supporting types

These are defined in `teatree/core/overlay.py`:

| Type | Kind | Fields |
|------|------|--------|
| `ProvisionStep` | dataclass | `name`, `callable`, `required`, `description` |
| `SymlinkSpec` | TypedDict | `path`, `source`, `mode`, `description` |
| `ServiceSpec` | TypedDict | `shared`, `service`, `compose_file`, `start_command`, `readiness_check` |
| `DbImportStrategy` | TypedDict | `kind`, `source_database`, `shared_postgres`, `snapshot_tool`, `restore_order`, `notes`, `worktree_repo_path` |
| `SkillMetadata` | TypedDict | `skill_path`, `remote_patterns`, `trigger_index`, `resolved_requires`, `skill_mtimes`, `teatree_version` |
| `ToolCommand` | TypedDict | `name`, `help`, `command`, `arguments` |
| `ValidationResult` | TypedDict | `errors`, `warnings` |
| `RunCommand` | dataclass | `args`, `cwd` |
| `RunCommands` | type alias | `dict[str, list[str] \| RunCommand]` |
| `HealthCheck` | dataclass | `name`, `check`, `description` |
