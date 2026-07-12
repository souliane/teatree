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

Overlay-specific configuration (tokens, URLs, service credentials) lives on `OverlayConfig`, accessed as `overlay.config`. Configure via an `overlay_settings` module (Django-style) or via the overlay's `overlays` registry row in the DB `ConfigSetting` store.

### Static attributes

Set these as `UPPER_CASE` constants in a settings module, or as `lower_case` keys in TOML:

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `gitlab_url` | `"https://gitlab.com/api/v4"` | GitLab API base URL |
| `github_owner` | `""` | GitHub user or org that owns the project board |
| `github_project_number` | `0` | GitHub Projects v2 board number |
| `require_ticket` | `False` | Whether to enforce a tracked issue before coding/shipping |
| `known_variants` | `[]` | Tenant variant identifiers |
| `pr_auto_labels` | `[]` | Labels auto-applied to pull requests (GitLab MRs translated at the API edge) |
| `frontend_repos` | `[]` | Frontend repo names (for build steps) |
| `workspace_repos` | `[]` | Repo paths relative to `workspace_dir` (supports nested paths) |
| `protected_branches` | `[]` | Branch names that should never be deleted during cleanup |
| `dev_env_url` | `""` | Development environment base URL |

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
| `validate_pr(title, description)` | reject a title or a description **first line** not matching the effective `mr_title_regex` (Conventional Commits plus the release-notes types `improvement\|config\|techdebt` some overlays narrow to), a description missing a What/Why header (#1540, #1367), or a description missing any section declared in `get_required_description_sections()` (#312); the first-line check mirrors the GitLab `validate_mr_title_and_description` CI gate, which parses the literal first line and never falls back to the title | Validate PR title and description against project conventions; override to assemble a different grammar, but the default is a real gate enforced at `pr create` |
| `build_pr_title(branch, subject, body, issue_url)` | the commit `subject` | Produce the PR title from structured ticket data so the ship path generates a compliant title instead of copying a raw subject |
| `get_required_description_sections()` | `[]` | MR-description sections (beyond What/Why) the gate requires and the generator emits by default — e.g. `["Configuration"]` (#312) |
| `get_description_section_defaults()` | `{}` | Default body text the generator writes under a missing required section — e.g. a `Configuration` no-config line (#312) |
| `get_followup_repos()` | `[]` | Repos to check during follow-up sync |
| `get_skill_metadata()` | `{}` | Skill path, remote patterns, trigger index for the overlay's companion skills |
| `get_ci_project_path()` | `""` | CI project path for pipeline triggers and evidence posting |
| `get_e2e_config()` | `{}` | E2E runner config: `runner` (`"project"` / `"external"`), plus `test_dir` / `settings_module` (project runner) or `project_path` / `ref` (external runner) |
| `detect_variant()` | `""` | Detect the current tenant variant from project context |
| `get_tool_commands()` | `[]` | Custom tool commands exposed via `t3 <overlay> tool run` |
| `get_issue_title(url)` | `""` | Fetch the title of an issue from its URL |

## `OverlayBase`

`OverlayBase` composes its extension surface: `config` (`OverlayConfig`),
`metadata` (`OverlayMetadata`), `provisioning` (`OverlayProvisioning`), `runtime`
(`OverlayRuntime`), `e2e` (`OverlayE2E`), `review` (`OverlayReview`), and
`connectors` (`OverlayConnectors`) are attributes on the base. The provisioning,
run, e2e, review, and connector hooks live on those providers — reached as
`overlay.provisioning.*`, `overlay.runtime.*`, `overlay.review.*`, and so on, with
NO `get_` prefix — not directly on `OverlayBase`. Override a hook by assigning your
own provider subclass to the corresponding attribute, exactly the way `config` and
`metadata` are subclassed above.

The generated [`overlay-extension-points.md`](generated/overlay-extension-points.md)
is the always-current, machine-derived hook list; the tables below give the shape
and defaults.

### Mandatory hooks (on `OverlayBase`)

These are abstract -- you must implement them.

#### `get_repos() -> list[str]`

Return the list of repository names your project manages. Teatree uses this to know which repos to create worktrees in.

#### `get_provision_steps(worktree: Worktree) -> list[ProvisionStep]`

Return the ordered steps to provision a worktree after creation. Each step is a `ProvisionStep` with a name, callable, and optional description. Steps run sequentially during `worktree provision`.

### Other hooks on `OverlayBase`

These have default implementations. Override them as needed.

| Method | Default | Purpose |
|--------|---------|---------|
| `get_workspace_repos()` | `get_repos()` | Repo paths relative to `workspace_dir`; supports nested paths (e.g. `souliane/teatree`). Reads `config.workspace_repos` first, else `get_repos()`. |
| `get_issue_title(url)` | `""` | Fetch an issue's title from its URL via the resolved code host. |
| `is_issue_done(issue_data)` | `state ∈ {closed, completed}` | Whether an issue's raw API payload indicates the work is complete. |
| `resolve_mr_token(iid)` | ref store → constructed URL | Canonical URL for `!<iid>` on this overlay's code host, or `None`. |
| `resolve_issue_token(iid)` | ref store → constructed URL | Canonical URL for `#<iid>`, same contract as `resolve_mr_token`. |
| `get_timeouts()` | `{}` | Timeout overrides in seconds keyed by `teatree.timeouts` operation name; `0` disables a timeout. |
| `get_health_signals()` | `[]` | Overlay operational-health signals for the global aggregator. |
| `get_checking_sources()` | `[]` | Extra "needs you" source identifiers for `t3 <overlay> checking show`. |
| `get_eval_scenarios_dir()` | `None` | Package-relative directory of overlay-contributed behavioral eval scenarios. |

### Provisioning hooks (`overlay.provisioning`, `OverlayProvisioning`)

Worktree setup + environment. Override by assigning an `OverlayProvisioning` subclass to `OverlayBase.provisioning`.

| Method | Default | Purpose |
|--------|---------|---------|
| `env_extra(worktree)` | `{}` | Extra environment variables to set for a worktree. |
| `declared_env_keys()` | `set()` | Env keys the overlay declares it writes (for env-cache validation). |
| `declared_secret_env_keys()` | `{POSTGRES_PASSWORD}` | Env keys whose values are secrets (redacted in diagnostics). |
| `db_import_strategy(worktree)` | `None` | How to import/restore a database for this worktree; `None` = no DB import. |
| `db_import(worktree, *, force, slow_import, dslr_snapshot, dump_path, approve_remote_dump)` | `False` | Run the actual database import. Called by `worktree provision` and `db refresh`. `approve_remote_dump` is `True` only when the user approved a fresh remote DEV dump for this single invocation via the `db refresh --fresh-dump` gate — an unattended agent cannot satisfy it, so it still cannot self-trigger a network pg_dump. |
| `post_db_steps(worktree)` | `[]` | Steps to run after a database import (migrations, data fixups). |
| `reset_passwords_command(worktree)` | `None` | A provision step that resets user passwords to a known dev value; run by `db reset-passwords`. |
| `envrc_lines(worktree)` | `[]` | Extra lines to append to the worktree's `.envrc`. |
| `symlinks(worktree)` | `[]` | Symlinks to create in the worktree (shared config, node_modules). |
| `services_config(worktree)` | `{}` | Service config (compose files, readiness checks, shared vs. per-worktree). |
| `compose_file(worktree)` | `""` | Path to the docker-compose file; used by `worktree start` and `run backend`. |
| `base_images(worktree)` | `[]` | Docker base images teatree builds once and shares across worktrees. |
| `docker_services(worktree)` | `set()` | Service names that MUST run in Docker — enforced at `worktree provision`. |
| `cleanup_steps(worktree)` | `[]` | Extra cleanup steps run before a worktree is removed. |
| `health_checks(worktree)` | default set | Post-provision health checks (path exists, symlinks valid, DB name set). |
| `snapshot_warmer_configs()` | `[]` | Reference-DB configs the snapshot-warmer loop keeps current, one per variant. |
| `reap_external_resources(worktree)` | `[]` | Out-of-band resources a reaped worktree leaves behind (compose containers/images). |
| `resolve_variant(name)` | `Variant.bare(name)` | Resolve a variant name into a first-class `Variant` (tenant / language / DSLR snapshot / E2E creds). |

### Run hooks (`overlay.runtime`, `OverlayRuntime`)

Running services, tests, and readiness probes. Override by assigning an `OverlayRuntime` subclass to `OverlayBase.runtime`.

| Method | Default | Purpose |
|--------|---------|---------|
| `run_commands(worktree)` | `{}` | Named service commands (backend, frontend, build-frontend, …) for `worktree start`. |
| `pre_run_steps(worktree, service)` | `[]` | Steps to run before starting a specific service (copy config, refresh translations). |
| `test_command(worktree)` | `[]` | The command to run the project test suite; used by `run tests`. |
| `lint_command(worktree)` | `[]` | The command to lint the worktree; used by `run lint`. When empty, `run lint` exits non-zero so a caller is not told green. |
| `verify_endpoints(worktree)` | `{}` | Health-check URL paths per service keyed by `worktree.ports` entry; unlisted services fall back to `/`. |
| `readiness_probes(worktree)` | `[]` | Post-start runtime probes gating the `services_up → ready` transition. |

### E2E hooks (`overlay.e2e`, `OverlayE2E`)

| Method | Default | Purpose |
|--------|---------|---------|
| `env_extras(env_cache)` | `{}` | Extra env for the e2e runner derived from the worktree env cache. |
| `run_provenance(spec_path)` | `""` | Manifest entry id (e.g. CI lane) recorded on the run for a spec. |
| `playwright_args(spec_path)` | `[]` | Extra `npx playwright test` CLI args for a spec (e.g. `-c <config>`). |
| `scenarios(spec_path)` | `()` | Per-feature acceptance scenarios for the templated-test-plan renderer. |
| `preflight(*, customer, base_url)` | `[]` | Zero-arg probes run before an e2e run; each raises when a dependency is unreachable. |

### Review hooks (`overlay.review`, `OverlayReview`)

| Method | Default | Purpose |
|--------|---------|---------|
| `merge_candidate_repo_slugs()` | `[]` | Static working-repo slugs the cross-repo merge probe binds against. |
| `can_auto_merge(*, target_ref, thread_ref)` | `MergeGuard(allowed=True)` | Verdict on whether an approved merge request may auto-merge. Override to enforce human-approval gates, freeze windows, or policy checks — return `MergeGuard(allowed=False, reason=…)` to block, adding `escalate=True` to raise an escalation instead of a silent block. |
| `visual_qa_targets(changed_files)` | `[]` | Files whose change warrants a visual-QA pass. |
| `classify_customer_display_impact(changed_files)` | `True` (fail-closed) | Whether a diff could impact what the customer sees; the mandatory-E2E gate reads it, so the default treats every diff as display-impacting. |

### Connector hooks (`overlay.connectors`, `OverlayConnectors`)

| Method | Default | Purpose |
|--------|---------|---------|
| `preflight()` | `[]` | Zero-arg probes run before connector-dependent loop work; each raises when a hard-depended connector is unreachable. |
| `mcp_provider_expectations()` | `{}` | `{mcp_server_name: provider}` for the connectivity check. |
| `manifest()` | `[]` | The overlay's required-vs-optional claude.ai connectors by name. |

## Supporting types

Most of these are defined in `teatree/types.py` (the Django-free shared types module). `HealthCheck` lives in `teatree/core/worktree/health.py` and `MergeGuard` in `teatree/core/gates/merge_guard.py`:

| Type | Kind | Fields |
|------|------|--------|
| `ProvisionStep` | dataclass | `name`, `callable`, `required`, `description` |
| `SymlinkSpec` | TypedDict | `path`, `source`, `mode`, `description` |
| `ServiceSpec` | TypedDict | `shared`, `service`, `compose_file`, `start_command`, `readiness_check`, `base_image` |
| `DbImportStrategy` | TypedDict | `kind`, `source_database`, `shared_postgres`, `snapshot_tool`, `restore_order`, `notes`, `worktree_repo_path` |
| `SkillMetadata` | TypedDict | `skill_path`, `remote_patterns`, `skill_index`, `resolved_requires`, `skill_mtimes`, `teatree_version` |
| `ToolCommand` | TypedDict | `name`, `help`, `command`, `arguments` |
| `ValidationResult` | TypedDict | `errors`, `warnings` |
| `RunCommand` | dataclass | `args`, `cwd` |
| `RunCommands` | type alias | `dict[str, list[str] \| RunCommand]` |
| `HealthCheck` | dataclass | `name`, `check`, `description` |
| `MergeGuard` | dataclass (frozen) | `allowed`, `reason`, `escalate` |

## Ship your own harness (headless factory overlays, #3157)

The headless agent runtime drives an in-process agent session behind the
`teatree.agents.harness.Harness` protocol (`open(options) -> HarnessSession`). The backend
set is **open**: an overlay registers a third transport (a direct Anthropic Messages-API
backend, an enterprise cloud endpoint, a self-hosted model) with **zero core edits** via the
`teatree.harnesses` entry-point group. All the factory-facing symbols are re-exported from
`teatree.overlay_sdk` — an overlay never imports the private `teatree.agents._*` internals (an
import-linter contract forbids it).

```python
# my_overlay/harness.py
import contextlib
from teatree.overlay_sdk import HarnessCapabilities, HarnessSpec, HarnessBuildContext

class MyHarness:
    capabilities = HarnessCapabilities(
        hooks=False, mcp=True, cache_control=True, server_resume=False, structured_output=True,
    )
    def __init__(self, ctx: HarnessBuildContext) -> None: ...
    @contextlib.asynccontextmanager
    async def open(self, options): ...  # yield a HarnessSession

def my_harness_spec() -> HarnessSpec:
    return HarnessSpec(
        name="my_harness",
        factory=lambda ctx: MyHarness(ctx),
        capabilities=MyHarness.capabilities,
        valid_providers=frozenset({"anthropic_api"}),
    )
```

```toml
# my_overlay/pyproject.toml
[project.entry-points."teatree.harnesses"]
my_harness = "my_overlay.harness:my_harness_spec"
```

Select it with `t3 <overlay> config_setting set agent_harness my_harness`. The dispatch path
resolves the backend from the registry and reads its `capabilities` — it never
`isinstance`-branches on a concrete harness class. A factory overlay also drives the full
dispatch → attempt → cost cycle through the SDK: `run_headless`, `record_result_envelope` /
`AttemptUsage`, `headless_cost_breakdown`, plus `ContextPlan` (cache-control breakpoints on
the direct-API binding), `CompactionPolicy`, `TicketBudget` / `LoopWatchdog`, and
`build_lane_b_toolsets` — all from `teatree.overlay_sdk`.
