# Overlay Extension Points

Base class: `teatree.core.overlay.OverlayBase`

## Hooks

| Hook | Required | Signature | Description |
| --- | --- | --- | --- |
| `get_repos` | Yes | `() -> list[str]` | Declare the repositories that TeaTree should provision for this overlay. |
| `get_provision_steps` | Yes | `(worktree: 'Worktree') -> list[teatree.core.overlay.ProvisionStep]` | Return the ordered setup steps for a newly created worktree. |
| `get_env_extra` | No | `(_worktree: 'Worktree') -> dict[str, str]` | Add overlay-specific environment variables to the generated worktree env file. |
| `get_run_commands` | No | `(_worktree: 'Worktree') -> RunCommands` | Expose named service commands for lifecycle start and operator discovery. |
| `get_db_import_strategy` | No | `(_worktree: 'Worktree') -> DbImportStrategy` | Describe how a worktree database should be provisioned or restored. |
| `get_post_db_steps` | No | `(_worktree: 'Worktree') -> list[HookValue]` | Return callbacks to run after database setup completes. |
| `get_symlinks` | No | `(_worktree: 'Worktree') -> list[HookValue]` | Declare extra symlinks that should exist inside the worktree. |
| `get_services_config` | No | `(_worktree: 'Worktree') -> dict[str, HookValue]` | Return additional service metadata for lifecycle orchestration. |
| `validate_mr` | No | `(_title: str, _description: str) -> list[str]` | Return merge-request validation problems for this overlay. |
| `get_skill_metadata` | No | `() -> dict[str, HookValue]` | Return the active overlay skill path and any companion skills. |

## Settings

| Setting | Required | Description |
| --- | --- | --- |
| `TEATREE_OVERLAY_CLASS` | Yes | Import path for the active OverlayBase subclass. |
| `TEATREE_SDK_RUNTIME` | Yes | Runtime key for unattended SDK execution. |
| `TEATREE_INTERACTIVE_RUNTIME` | Yes | Runtime key for interactive user-input work. |
| `TEATREE_TERMINAL_MODE` | Yes | Terminal strategy used by the interactive runtime. |

## Runtime Commands

- `lifecycle setup`
- `lifecycle start`
- `lifecycle status`
- `lifecycle teardown`
- `tasks work-next-sdk`
- `tasks work-next-user-input`
- `followup refresh`
- `followup remind`

## Skill Metadata

| Field | Required | Description |
| --- | --- | --- |
| `skill_path` | No | Primary overlay skill file path. |
| `companion_skills` | No | Additional skills loaded alongside the primary overlay skill. |
