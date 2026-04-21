# Management Commands

Teatree's Django management commands handle all database-touching operations. They use [django-typer](https://github.com/bckohan/django-typer) so each command exposes typed subcommands.

These are also accessible through the `t3` CLI (see [CLI Reference](cli.md)), but you can call them directly with `manage.py` if you prefer.

## `lifecycle`

Manages worktree state transitions.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `setup` | `--path`, `--variant`, `--overlay`, `--slow-import`, `--verbose`, `--no-timeout` | worktree ID | Provisions all worktrees in the ticket (DB name, env file, overlay steps). Idempotent — safe to re-run. |
| `start` | `--path`, `--variant`, `--overlay`, `--verbose`, `--no-timeout` | state string | Runs setup (idempotent), allocates ports, starts docker-compose services for all ticket worktrees |
| `status` | `--path` | dict | Returns current state, repo path, branch, and allocated ports |
| `teardown` | `--path` | state string | Stops containers, resets worktree state to `created` |
| `clean` | `--path` | string | Stops containers, drops DB, resets worktree state |
| `diagnose` | `--path` | dict | Checks worktree health: git dir, env file, DB, docker services |
| `smoke-test` | -- | dict | Quick health check: overlay loads, CLI responds, imports OK, database accessible |
| `visit-phase` | `ticket_id`, `phase` | string | Marks a phase as visited on the ticket's latest session |
| `diagram` | `model` (`worktree`/`ticket`/`task`), `--ticket` | string | Prints a Mermaid state diagram for the given model or ticket lifecycle |

## `workspace`

Ticket and workspace management.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `ticket` | `issue_url`, `variant`, `repos`, `description` | ticket ID | Creates or updates a ticket with worktree entries for each repo. Idempotent — safe to re-run after partial failures. |
| `finalize` | `ticket_id`, `--message` | string | Squashes worktree commits into one, then rebases on the default branch |
| `clean-all` | `--keep-dslr` | list of strings | Prunes merged worktrees, stale branches, orphaned stashes, orphan databases, and old DSLR snapshots |

## `db`

Database operations on worktrees.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `refresh` | `--path`, `--dslr-snapshot`, `--dump-path`, `--force` | string | Re-imports the worktree database from DSLR snapshot or dump, runs post-DB steps and password reset |
| `restore-ci` | `--path` | string | Restores the worktree database from the latest CI dump |
| `reset-passwords` | `--path` | string | Resets all user passwords to a known dev value via the overlay's reset command |

## `run`

Service management.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `verify` | `--path` | dict | Checks that dev services respond via HTTP, then advances FSM. Discovers ports from docker-compose. |
| `services` | `--path` | `RunCommands` | Returns the overlay's run commands for this worktree |
| `backend` | `--path` | string | Starts the backend via docker-compose with allocated ports |
| `frontend` | `--path` | string | Starts the frontend dev server on the host (background process) with dynamic port allocation |
| `build-frontend` | `--path` | string | Builds the frontend app for production/testing via the overlay's `build-frontend` command |
| `tests` | `--path`, `-- <extra args>` | string | Runs the project test suite. Extra arguments after `--` are appended to the test command. |

## `pr`

Pull/merge request operations.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `create` | `ticket_id`, `repo`, `title`, `description`, `--dry-run`, `--skip-validation` | dict | Creates a merge request for the ticket's branch. Auto-fills title/description from last commit. Checks shipping gates before creating. |
| `check-gates` | `ticket_id`, `target_phase` | dict | Checks whether session gates allow a phase transition (default: `shipping`) |
| `fetch-issue` | `issue_url` | dict | Fetches issue details with embedded image URLs and external links (Notion, Linear, Jira) |
| `detect-tenant` | -- | string | Detects the current tenant variant from the overlay |
| `post-evidence` | `mr_iid`, `repo`, `title`, `body`, `files` | dict | Posts test evidence as an MR comment. Uploads files and updates existing `## Test Plan` notes. |

## `tasks`

Task queue for multi-agent coordination.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `claim` | `execution_target`, `claimed_by` | task ID or None | Claims the next pending task of the given target type |
| `work-next-sdk` | `claimed_by` | dict or None | Claims and completes the next SDK task using the configured runtime |
| `work-next-user-input` | `claimed_by` | dict or None | Claims and completes the next user-input task |
| `cancel` | `task_id`, `--confirm` | None | Cancels a pending or claimed task. Requires `--confirm` for claimed tasks. |
| `list` | `--status`, `--execution-target` | list of dicts | Lists tasks, optionally filtered by status and/or execution target |

## `followup`

Monitoring and reminders.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `refresh` | -- | dict | Returns counts of tickets, tasks, and open tasks |
| `sync` | -- | dict | Syncs follow-up data: discovered MRs, created/updated tickets, errors |
| `remind` | -- | list of IDs | Returns IDs of pending interactive tasks |

## `ticket`

Ticket state management.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `transition` | `ticket_id`, `transition_name` | dict | Transitions a ticket to a new state. Allowed transitions: `scope`, `start`, `code`, `test`, `review`, `ship`, `request_review`, `mark_merged`, `retrospect`, `mark_delivered`, `rework`. |
| `list` | `--state`, `--overlay` | list of dicts | Lists tickets, optionally filtered by state and/or overlay |

## `e2e`

End-to-end test execution.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `trigger-ci` | `branch` | dict | Triggers E2E tests on a remote CI pipeline via the overlay's E2E config |
| `external` | `test_path`, `--headed`, `--update-snapshots` | string | Runs Playwright tests from the external test repo (`T3_PRIVATE_TESTS`). Auto-discovers frontend port and tenant variant. |
| `project` | `test_path`, `--headed`, `--docker` | string | Runs E2E tests from the project's own test directory, via docker or directly |

## `tool`

Overlay-specific tool commands.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `run` | `name`, `-- <extra args>` | string | Runs an overlay tool command by name. Extra arguments are forwarded. |
| `list` | -- | string | Lists available overlay tool commands |

## `generate_all_docs`

Documentation generation.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output_dir` | string | Generates all docs: overlay extension-points, skill delegation matrix, CLI reference |

## `generate_cli_docs`

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output` | string | Generates CLI reference documentation from `--help` introspection |

## `generate_overlay_docs`

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output_dir` | string | Generates deterministic overlay extension-point documentation (JSON + Markdown) |

## `generate_skill_docs`

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output_dir`, `skill_map` | string | Generates deterministic skill delegation documentation (JSON + Markdown) |
