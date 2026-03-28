# Management Commands

Teatree's Django management commands handle all database-touching operations. They use [django-typer](https://github.com/bckohan/django-typer) so each command exposes typed subcommands.

## lifecycle

| Subcommand | Description |
|-----------|-------------|
| `setup` | Create worktree, provision, run overlay steps |
| `start` | Start backend/frontend services |
| `status` | Show worktree state |
| `teardown` | Tear down worktree |
| `clean` | Full teardown + state cleanup |
| `diagram` | Render Mermaid state diagram from FSM |

## workspace

| Subcommand | Description |
|-----------|-------------|
| `ticket` | Create ticket + worktrees for all repos |
| `inspect` | Show ticket/worktree details |

## db

| Subcommand | Description |
|-----------|-------------|
| `create` | Create database |
| `refresh` | Re-import from snapshot/dump |
| `export` | Export database |
| `import` | Import database |
| `restore-ci` | Restore from CI artifact |

## run

| Subcommand | Description |
|-----------|-------------|
| `backend` | Start backend service |
| `frontend` | Start frontend service |
| `build-frontend` | Build frontend |
| `tests` | Run test suite |
| `e2e` | Run E2E tests |

## tasks

| Subcommand | Description |
|-----------|-------------|
| `claim` | Claim next pending task |
| `work-next-sdk` | Execute headless task |
| `work-next-user-input` | Create interactive session |

## followup

| Subcommand | Description |
|-----------|-------------|
| `refresh` | Count pending work |
| `sync` | Sync from GitLab |
| `discover-mrs` | Discover open MRs |
| `remind` | Send reminders |

## pr

| Subcommand | Description |
|-----------|-------------|
| `create` | Create MR/PR |
| `fetch-issue` | Fetch issue details |
| `detect-tenant` | Detect tenant variant |
| `post-evidence` | Post evidence to MR |
