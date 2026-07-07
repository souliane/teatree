# Overlay Extension Points

Base class: `teatree.core.overlay.OverlayBase`

## Hooks

| Hook | Required | Signature | Description |
| --- | --- | --- | --- |
| `get_repos` | Yes | `() -> list[str]` | Declare the repositories that TeaTree should provision for this overlay. |
| `get_provision_steps` | Yes | `(worktree: 'Worktree') -> list[teatree.types.ProvisionStep]` | Return the ordered setup steps for a newly created worktree. |
| `get_env_extra` | No | `(worktree: 'Worktree') -> dict[str, str]` | Add overlay-specific environment variables to the generated worktree env file. |
| `get_run_commands` | No | `(worktree: 'Worktree') -> RunCommands` | Expose named service commands for `worktree start` and operator discovery. |
| `get_db_import_strategy` | No | `(worktree: 'Worktree') -> teatree.types.DbImportStrategy \| None` | Describe how a worktree database should be provisioned or restored. |
| `get_post_db_steps` | No | `(worktree: 'Worktree') -> list[teatree.types.ProvisionStep]` | Return callbacks to run after database setup completes. |
| `get_symlinks` | No | `(worktree: 'Worktree') -> list[teatree.types.SymlinkSpec]` | Declare extra symlinks that should exist inside the worktree. |
| `get_services_config` | No | `(worktree: 'Worktree') -> dict[str, teatree.types.ServiceSpec]` | Return additional service metadata for worktree-lifecycle orchestration. |
| `get_base_images` | No | `(worktree: 'Worktree') -> list[teatree.types.BaseImageConfig]` | Declare Docker base images teatree builds once and shares across worktrees. |
| `get_docker_services` | No | `(worktree: 'Worktree') -> set[str]` | Declare service names that MUST run in Docker — enforced at `worktree provision`. |
| `reap_worktree_external_resources` | No | `(worktree: 'Worktree') -> list[str]` | Reap a reaped worktree's out-of-band resources (e.g. its docker compose containers + images). |
| `get_checking_sources` | No | `() -> list[str]` | Return extra 'needs you' source identifiers for the `t3 <overlay> checking show` report. |
| `metadata.validate_pr` | No | `(title: str, description: str) -> teatree.types.ValidationResult` | Return PR validation problems for this overlay. |
| `metadata.build_pr_title` | No | `(*, branch: str, subject: str, body: str, issue_url: str) -> str` | Produce the PR title from structured ticket data (default: the commit subject). |
| `metadata.get_required_description_sections` | No | `() -> list[str]` | Declare MR-description sections (beyond What/Why) the gate requires and the generator emits. |
| `metadata.get_description_section_defaults` | No | `() -> dict[str, str]` | Map a required section to the default body the generator writes when the commit omits it. |
| `metadata.get_skill_metadata` | No | `() -> teatree.types.SkillMetadata` | Return the active overlay skill path and remote match patterns. |
| `metadata.get_ci_project_path` | No | `() -> str` | Return the GitLab project path for CI operations. |
| `metadata.get_e2e_config` | No | `() -> dict[str, str]` | Return E2E runner configuration (runner, test_dir, settings_module, project_path, ref). |
| `metadata.detect_variant` | No | `() -> str` | Detect the current tenant variant from environment. |
| `metadata.get_tool_commands` | No | `() -> list[teatree.types.ToolCommand]` | Return overlay-specific tool commands for t3 <overlay> tool. |
| `metadata.get_followup_repos` | No | `() -> list[str]` | Return GitLab project paths to sync MRs from. |

## Settings

| Setting | Required | Description |
| --- | --- | --- |

## Runtime Commands

- `worktree provision`
- `worktree start`
- `worktree status`
- `worktree teardown`
- `tasks work-next-headless`
- `followup refresh`
- `followup remind`
- `checking show`

## Skill Metadata

| Field | Required | Description |
| --- | --- | --- |
| `skill_path` | No | Primary overlay skill file path. |
| `remote_patterns` | No | Git remote patterns that activate the overlay skill outside the host project. |
