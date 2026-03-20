# Management Commands

Teatree's Django management commands handle all database-touching operations. They use [django-typer](https://github.com/bckohan/django-typer) so each command exposes typed subcommands.

These are also accessible through the `t3` CLI (see [CLI Reference](cli.md)), but you can call them directly with `manage.py` if you prefer.

## `lifecycle`

Manages worktree state transitions.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `setup` | `ticket_id`, `repo_path`, `branch` | worktree ID | Creates a worktree, provisions it (port allocation, DB name), then runs overlay provision steps |
| `start` | `worktree_id` | state string | Fetches run commands from the overlay, records services, transitions to `services_up` |
| `status` | `worktree_id` | dict | Returns current state, repo path, and branch |
| `teardown` | `worktree_id` | state string | Clears ports, DB name, and facts; resets to `created` |

## `workspace`

Ticket and workspace management.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `ticket` | `issue_url`, `variant`, `repos` | ticket ID | Creates a ticket, scopes it, and transitions to `started` |
| `finalize` | `ticket_id` | state string | Transitions the ticket to `coded` |
| `clean-all` | -- | count | Deletes all worktrees in `created` state |

## `db`

Database operations on worktrees.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `refresh` | `worktree_id` | state string | Transitions to `provisioned` and records refresh timestamp |
| `status` | `worktree_id` | dict | Returns DB name, state, and last refresh time |

## `run`

Service management.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `verify` | `worktree_id` | dict | Transitions to `ready`, records backend/frontend URLs |
| `services` | `worktree_id` | dict | Returns the overlay's run commands for this worktree |

## `mr`

Merge request operations.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `validate` | `title`, `description` | `ValidationResult` | Runs the overlay's MR validation rules |
| `check-gates` | `ticket_id` | bool | Verifies the most recent session has passed all required quality gates for shipping |

## `tasks`

Task queue for multi-agent coordination.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `claim` | `execution_target`, `claimed_by` | task ID or None | Claims the next pending task of the given target type |
| `work-next-sdk` | `claimed_by` | dict or None | Claims and completes the next SDK task using the configured runtime |
| `work-next-user-input` | `claimed_by` | dict or None | Claims and completes the next user-input task |

## `followup`

Monitoring and reminders.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `refresh` | -- | dict | Returns counts of tickets, tasks, and open tasks |
| `remind` | -- | list of IDs | Returns IDs of pending user-input tasks |
