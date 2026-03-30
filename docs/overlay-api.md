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

## Configuration methods

Overlay-specific configuration (tokens, URLs, service credentials) lives on `OverlayBase` methods rather than Django settings. Override these in your subclass:

| Method | Default | Purpose |
|--------|---------|---------|
| `get_gitlab_token()` | `""` | GitLab API token |
| `get_gitlab_url()` | `"https://gitlab.com/api/v4"` | GitLab API base URL |
| `get_gitlab_username()` | `""` | GitLab username for MR assignment |
| `get_slack_token()` | `""` | Slack bot token for notifications |
| `get_review_channel()` | `("", "")` | `(channel_name, channel_id)` for review notifications |
| `get_known_variants()` | `[]` | Tenant variant identifiers |
| `get_mr_auto_labels()` | `[]` | Labels auto-applied to merge requests |
| `get_frontend_repos()` | `[]` | Frontend repo names (for build steps) |
| `get_dev_env_url()` | `""` | Development environment base URL |
| `get_dashboard_logo()` | `""` | Path or URL for the dashboard logo |

These replace the old `TEATREE_*` Django settings. Each overlay carries its own configuration, so multi-overlay setups can point to different GitLab instances or Slack workspaces.

## `OverlayBase`

### Mandatory hooks

These are abstract -- you must implement them.

#### `get_repos() -> list[str]`

Return the list of repository names your project manages. Teatree uses this to know which repos to create worktrees in.

#### `get_provision_steps(worktree: Worktree) -> list[ProvisionStep]`

Return the ordered steps to provision a worktree after creation. Each step is a `ProvisionStep` with a name, callable, and optional description. Steps run sequentially during `lifecycle setup`.

### Optional hooks

These have default implementations that return empty/neutral values. Override them as needed.

#### `get_env_extra(worktree: Worktree) -> dict[str, str]`

Extra environment variables to set for a worktree. Defaults to `{}`.

#### `get_run_commands(worktree: Worktree) -> RunCommands`

Commands to run services (backend, frontend, etc.) for a worktree. Returns a dict mapping service name to shell command. Defaults to `{}`.

#### `get_db_import_strategy(worktree: Worktree) -> DbImportStrategy | None`

How to import/restore a database for this worktree. Returns `None` if no DB import is needed.

#### `get_post_db_steps(worktree: Worktree) -> list[PostDbStep]`

Steps to run after a database import (migrations, data fixups, password resets). Defaults to `[]`.

#### `get_symlinks(worktree: Worktree) -> list[SymlinkSpec]`

Symlinks to create in the worktree (e.g., shared config files, node_modules). Defaults to `[]`.

#### `get_services_config(worktree: Worktree) -> dict[str, ServiceSpec]`

Service configuration (Docker compose files, readiness checks, shared vs. per-worktree). Defaults to `{}`.

#### `validate_mr(title: str, description: str) -> ValidationResult`

Validate a merge request title and description against project conventions. Returns a `ValidationResult` with `errors` and `warnings` lists. Defaults to no errors or warnings.

#### `get_skill_metadata() -> SkillMetadata`

Return metadata about the overlay's companion skills (skill path, related skill names). Defaults to `{}`.

## Supporting types

These are `TypedDict` classes defined in `teatree/core/overlay.py`:

| Type | Fields |
|------|--------|
| `ProvisionStep` | `name`, `callable`, `required`, `description` |
| `PostDbStep` | `name`, `description`, `command` |
| `SymlinkSpec` | `path`, `source`, `mode`, `description` |
| `ServiceSpec` | `shared`, `service`, `compose_file`, `start_command`, `readiness_check` |
| `DbImportStrategy` | `kind`, `source_database`, `shared_postgres`, `snapshot_tool`, `restore_order`, `notes`, `worktree_repo_path` |
| `SkillMetadata` | `skill_path`, `companion_skills` |
| `ValidationResult` | `errors`, `warnings` |
| `RunCommands` | `dict[str, str]` (type alias) |
