# Extension Points — OverlayBase API

Project-specific behavior lives in overlay packages. Each overlay subclasses `OverlayBase` and uses composition:

- `overlay.config` — `OverlayConfig` instance (credentials, URLs, labels, settings)
- `overlay.metadata` — `OverlayMetadata` instance (CI, MR, skills, tool commands)

## Architecture

```
OverlayBase
  ├── config: OverlayConfig      (settings_module + ~/.teatree.toml overrides)
  │     ├── get_gitlab_token()    (dynamic from *_PASS_KEY convention)
  │     ├── get_github_token()
  │     ├── github_owner          (class-level constant)
  │     └── ...
  ├── metadata: OverlayMetadata
  │     ├── get_skill_metadata()
  │     ├── get_ci_project_path()
  │     └── ...
  └── instance methods            (worktree-scoped hooks)
        ├── get_repos()
        ├── get_provision_steps()
        └── ...
```

## OverlayConfig Methods (credentials & settings)

Settings are defined in `overlay_settings.py` and overridden per-user in `~/.teatree.toml`.

| Method / Attribute | Source | Purpose |
|---|---|---|
| `get_gitlab_token()` | `GITLAB_TOKEN_PASS_KEY` | GitLab API authentication |
| `get_gitlab_username()` | `GITLAB_USERNAME` | MR author filtering |
| `get_github_token()` | `GITHUB_TOKEN_PASS_KEY` | GitHub API authentication |
| `get_slack_token()` | `SLACK_TOKEN_PASS_KEY` | Slack notifications |
| `get_review_channel()` | `REVIEW_CHANNEL_ID/NAME` | Review request target |
| `gitlab_url` | `GITLAB_URL` | GitLab instance base URL |
| `known_variants` | `KNOWN_VARIANTS` | Multi-tenant variant list |
| `mr_auto_labels` | `MR_AUTO_LABELS` | Labels applied to new MRs |
| `frontend_repos` | `FRONTEND_REPOS` | Repos that need E2E tests |
| `dev_env_url` | `DEV_ENV_URL` | Development environment URL |
| `dashboard_logo` | `DASHBOARD_LOGO` | Custom dashboard branding |
| `github_owner` | `GITHUB_OWNER` | GitHub org/user for API calls |
| `github_project_number` | `GITHUB_PROJECT_NUMBER` | GitHub Projects v2 board number |
| `require_ticket` | `REQUIRE_TICKET` | Enforce ticket for all changes |

## OverlayMetadata Methods (CI, MR, skills)

| Method | Default | Override for... |
|---|---|---|
| `get_followup_repos()` | `[]` | Repos to sync MRs from |
| `get_skill_metadata()` | `{}` | Skill delegation and phase mapping |
| `get_ci_project_path()` | `""` | CI project path for pipeline triggers |
| `get_e2e_config()` | `{}` | E2E test runner configuration |
| `get_tool_commands()` | `[]` | Overlay-specific CLI tool commands |

## OverlayBase Instance Methods (worktree-scoped hooks)

| Method | Default | Override for... |
|---|---|---|
| `get_repos()` | `[]` | Repos to create worktrees for |
| `get_workspace_repos()` | `get_repos()` | Repos available in the workspace |
| `get_provision_steps(wt)` | `[]` | Post-setup steps (migrations, fixtures) |
| `get_env_extra(wt)` | `{}` | Extra env vars for worktree processes |
| `get_db_import_strategy(wt)` | `None` | DB import method (dslr, dump, none) |
| `db_import(wt)` | `False` | Execute DB import for the worktree |
| `get_post_db_steps(wt)` | `[]` | Post-DB-import steps (migrate, seed) |
| `get_reset_passwords_command(wt)` | `None` | Reset dev passwords |
| `get_envrc_lines(wt)` | `[]` | Lines for `.envrc` (direnv) |
| `get_symlinks(wt)` | `[]` | Symlinks from main repo to worktree |
| `get_services_config(wt)` | `{}` | Docker/background services to start |
| `get_run_commands(wt)` | `{}` | Dev server commands (backend, frontend) |
| `get_pre_run_steps(wt, service)` | `[]` | Steps before starting a service |
| `get_test_command(wt)` | `[]` | Test suite command |
| `get_verify_endpoints(wt)` | `{}` | Health check URLs for running services |
| `get_cleanup_steps(wt)` | `[]` | Steps when cleaning up a worktree |

## Creating an Overlay

```bash
uv run t3 startoverlay my-project ~/workspace/
```

This creates a minimal overlay package with:

- `overlay.py` — `OverlayBase` subclass
- `overlay_settings.py` — constants (credentials, repos, labels)
- `pyproject.toml` — entry point registration

Register the entry point:

```toml
[project.entry-points."teatree.overlays"]
t3-my-project = "my_project.overlay:MyProjectOverlay"
```

## Settings Resolution Order

1. `overlay_settings.py` constants (code defaults)
2. `~/.teatree.toml` `[overlays.<name>]` section (user overrides)
3. `*_PASS_KEY` convention auto-generates `get_*()` methods reading from `pass` store
