# Extension Points — OverlayBase API

Project-specific behavior lives in overlay packages. Each overlay subclasses `OverlayBase` and uses composition:

- `overlay.config` — `OverlayConfig` instance (credentials, URLs, labels, settings)
- `overlay.metadata` — `OverlayMetadata` instance (CI, PR, skills, tool commands)
- `overlay.provisioning` — `OverlayProvisioning` instance (worktree setup + environment)
- `overlay.runtime` — `OverlayRuntime` instance (running services, tests, readiness probes)
- `overlay.e2e` / `overlay.review` / `overlay.connectors` — the E2E, review, and connector concerns

The provisioning, run, e2e, review, and connector hooks live on those providers (with NO `get_` prefix), not directly on `OverlayBase`. The generated [`overlay-extension-points.md`](../../../docs/generated/overlay-extension-points.md) is the always-current, machine-derived hook list.

## Architecture

```
OverlayBase
  ├── config: OverlayConfig       (settings_module + DB overlays-registry overrides)
  │     ├── get_gitlab_token()     (dynamic from *_PASS_KEY convention)
  │     ├── get_github_token()
  │     ├── github_owner           (class-level constant)
  │     └── ...
  ├── metadata: OverlayMetadata
  │     ├── get_skill_metadata()
  │     ├── get_ci_project_path()
  │     └── ...
  ├── provisioning: OverlayProvisioning   (env_extra, db_import_strategy, symlinks, health_checks, ...)
  ├── runtime: OverlayRuntime             (run_commands, verify_endpoints, readiness_probes, ...)
  ├── e2e: OverlayE2E                     (env_extras, playwright_args, scenarios, ...)
  ├── review: OverlayReview               (can_auto_merge, classify_customer_display_impact, ...)
  ├── connectors: OverlayConnectors       (preflight, mcp_provider_expectations, manifest)
  └── mandatory hooks             (on OverlayBase)
        ├── get_repos()
        └── get_provision_steps()
```

## OverlayConfig Methods (credentials & settings)

Settings are defined in `overlay_settings.py` and overridden per-user in the DB `overlays` registry row.

| Method / Attribute | Source | Purpose |
|---|---|---|
| `get_gitlab_token()` | `GITLAB_TOKEN_PASS_KEY` | GitLab API authentication |
| `get_gitlab_username()` | `GITLAB_USERNAME` | PR author filtering |
| `get_github_token()` | `GITHUB_TOKEN_PASS_KEY` | GitHub API authentication |
| `get_slack_token()` | `SLACK_TOKEN_PASS_KEY` | Slack notifications |
| `get_review_channel()` | `REVIEW_CHANNEL_ID/NAME` | Review request target |
| `gitlab_url` | `GITLAB_URL` | GitLab instance base URL |
| `known_variants` | `KNOWN_VARIANTS` | Multi-tenant variant list |
| `pr_auto_labels` | `PR_AUTO_LABELS` | Labels applied to new PRs |
| `frontend_repos` | `FRONTEND_REPOS` | Repos that need E2E tests |
| `dev_env_url` | `DEV_ENV_URL` | Development environment URL |
| `github_owner` | `GITHUB_OWNER` | GitHub org/user for API calls |
| `github_project_number` | `GITHUB_PROJECT_NUMBER` | GitHub Projects v2 board number |
| `require_ticket` | `REQUIRE_TICKET` | Enforce ticket for all changes |

## OverlayMetadata Methods (CI, PR, skills)

| Method | Default | Override for... |
|---|---|---|
| `get_followup_repos()` | `[]` | Repos to sync PRs from |
| `get_skill_metadata()` | `{}` | Skill delegation and phase mapping |
| `get_ci_project_path()` | `""` | CI project path for pipeline triggers |
| `get_e2e_config()` | `{}` | E2E test runner configuration |
| `get_tool_commands()` | `[]` | Overlay-specific CLI tool commands |

## OverlayBase Hooks (mandatory + repo identity)

| Method | Default | Override for... |
|---|---|---|
| `get_repos()` | abstract | Repos to create worktrees for |
| `get_provision_steps(wt)` | abstract | Post-setup steps (migrations, fixtures) |
| `get_workspace_repos()` | `get_repos()` | Repos available in the workspace |

## `overlay.provisioning` Hooks (`OverlayProvisioning`)

| Method | Default | Override for... |
|---|---|---|
| `env_extra(wt)` | `{}` | Extra env vars for worktree processes |
| `db_import_strategy(wt)` | `None` | DB import method (dslr, dump, none) |
| `db_import(wt, *, ...)` | `False` | Execute DB import for the worktree |
| `post_db_steps(wt)` | `[]` | Post-DB-import steps (migrate, seed) |
| `reset_passwords_command(wt)` | `None` | Reset dev passwords |
| `envrc_lines(wt)` | `[]` | Lines for `.envrc` (direnv) |
| `symlinks(wt)` | `[]` | Symlinks from main repo to worktree |
| `services_config(wt)` | `{}` | Docker/background services to start |
| `compose_file(wt)` | `""` | docker-compose file path |
| `base_images(wt)` | `[]` | Docker base images shared across worktrees |
| `docker_services(wt)` | `set()` | Services that MUST run in Docker |
| `health_checks(wt)` | default set | Post-provision invariants |
| `cleanup_steps(wt)` | `[]` | Steps when cleaning up a worktree |

## `overlay.runtime` Hooks (`OverlayRuntime`)

| Method | Default | Override for... |
|---|---|---|
| `run_commands(wt)` | `{}` | Dev server commands (backend, frontend) |
| `pre_run_steps(wt, service)` | `[]` | Steps before starting a service |
| `test_command(wt)` | `[]` | Test suite command |
| `lint_command(wt)` | `[]` | Lint command |
| `verify_endpoints(wt)` | `{}` | Health check URLs for running services |
| `readiness_probes(wt)` | `[]` | Post-start runtime probes (gate `→ ready`) |

## Creating an Overlay

```bash
t3 startoverlay my-project ~/workspace/
```

This creates a minimal overlay package with:

- `src/<name>/overlay.py` — `OverlayBase` subclass
- `src/<name>/apps.py` — Django `AppConfig`
- `skills/t3:<base>/SKILL.md` — the overlay's companion skill
- `pyproject.toml` — entry point registration

Add an `overlay_settings.py` module (Django-style constants) yourself when you want file-authored config defaults, or set them in the DB `overlays` registry row.

Register the entry point:

```toml
[project.entry-points."teatree.overlays"]
t3-my-project = "my_project.overlay:MyProjectOverlay"
```

## Settings Resolution Order

1. `overlay_settings.py` constants (code defaults)
2. The DB `overlays` registry row for `<name>` (user overrides)
3. `*_PASS_KEY` convention auto-generates `get_*()` methods reading from `pass` store
