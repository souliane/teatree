# CLI Reference

Teatree provides a single `t3` entry point. Commands are split into two levels:

- **Global commands** — always available, no Django needed
- **Overlay commands** (`t3 <overlay> ...`) — registered per overlay, delegate to Django management commands

## Global Commands

### `t3 startproject`

Scaffold a new TeaTree overlay project.

```bash
t3 startproject <project_name> <destination> \
    --overlay-app <app_name> \
    [--project-package <package_name>]
```

### `t3 agent`

Launch Claude Code with auto-detected project context and skills.

```bash
t3 agent [TASK]
```

### `t3 sessions`

List recent Claude conversation sessions with resume commands.

```bash
t3 sessions [--all] [--limit N] [--project FILTER]
```

### `t3 overlays`

List installed overlays (from `~/.teatree.toml` and entry points).

### `t3 info`

Show t3 entry point, teatree/overlay sources, and editable status.

### `t3 docs`

Serve project documentation with mkdocs (requires `docs` dependency group).

### `t3 ci`

CI pipeline helpers.

| Subcommand | Description |
|------------|-------------|
| `cancel [BRANCH]` | Cancel stale CI pipelines |
| `divergence` | Check fork divergence from upstream |
| `fetch-errors [BRANCH]` | Fetch error logs from CI |
| `fetch-failed-tests [BRANCH]` | Extract failed test IDs from CI |
| `trigger-e2e [BRANCH]` | Trigger E2E tests on CI |
| `quality-check [BRANCH]` | Run quality analysis |

### `t3 review`

Code review helpers.

| Subcommand | Description |
|------------|-------------|
| `post-draft-note REPO MR NOTE` | Post a draft note on a GitLab MR |
| `delete-draft-note REPO MR NOTE_ID` | Delete a draft note |
| `list-draft-notes REPO MR` | List draft notes on an MR |

### `t3 review-request`

| Subcommand | Description |
|------------|-------------|
| `discover` | Discover open MRs awaiting review |

### `t3 tool`

Standalone utilities (no overlay needed).

| Subcommand | Description |
|------------|-------------|
| `privacy-scan [PATH]` | Scan text for privacy-sensitive patterns |
| `analyze-video VIDEO_PATH` | Decompose video into frames for AI analysis |
| `bump-deps` | Bump pyproject.toml dependencies from uv.lock |

### `t3 config`

| Subcommand | Description |
|------------|-------------|
| `write-skill-cache` | Write overlay skill metadata to cache |

### `t3 doctor`

| Subcommand | Description |
|------------|-------------|
| `check` | Verify imports and editable-install sanity |
| `repair` | Repair skill symlinks and verify installation health |

## Overlay Commands (`t3 <overlay> ...`)

Each registered overlay (e.g., `acme`) adds a subcommand group. These commands require the overlay project and delegate to Django management commands.

### Shortcuts

| Command | Purpose |
|---------|---------|
| `t3 <overlay> start-ticket <URL> [--variant V]` | Zero to coding — create ticket, provision worktree, start services |
| `t3 <overlay> ship <TICKET_ID> [--title T]` | Code to MR — create merge request for the ticket |
| `t3 <overlay> daily` | Daily followup — sync MRs, check gates, remind reviewers |
| `t3 <overlay> full-status` | Show ticket, worktree, and session state summary |
| `t3 <overlay> agent [TASK]` | Launch Claude Code with overlay context |
| `t3 <overlay> dashboard [--host H] [--port P]` | Start the dashboard dev server |
| `t3 <overlay> resetdb` | Drop and recreate the SQLite database |
| `t3 <overlay> worker [--count N] [--interval S]` | Start background task workers |

### `lifecycle` — Worktree state machine

| Command | Purpose |
|---------|---------|
| `t3 <overlay> lifecycle setup` | Create and provision a worktree |
| `t3 <overlay> lifecycle start` | Start services for a worktree |
| `t3 <overlay> lifecycle status` | Show current worktree state |
| `t3 <overlay> lifecycle teardown` | Tear down a worktree |
| `t3 <overlay> lifecycle clean` | Full teardown — stop services, drop DB, clean state |
| `t3 <overlay> lifecycle diagram` | Print the lifecycle state diagram as Mermaid |

### `workspace` — Workspace management

| Command | Purpose |
|---------|---------|
| `t3 <overlay> workspace ticket` | Create a ticket with worktree entries for each repo |
| `t3 <overlay> workspace finalize` | Squash worktree commits and rebase |
| `t3 <overlay> workspace clean-all` | Prune merged/gone worktrees and branches |

### `run` — Dev servers and test runners

| Command | Purpose |
|---------|---------|
| `t3 <overlay> run backend` | Start backend dev server |
| `t3 <overlay> run frontend` | Start frontend dev server |
| `t3 <overlay> run build-frontend` | Build frontend app |
| `t3 <overlay> run tests` | Run project tests |
| `t3 <overlay> run verify` | Verify dev services respond via HTTP |
| `t3 <overlay> run services` | Show configured run commands |
| `t3 <overlay> run e2e` | Run E2E tests via CI or overlay config |

### `db` — Database operations

| Command | Purpose |
|---------|---------|
| `t3 <overlay> db refresh` | Re-import database from dump/DSLR |
| `t3 <overlay> db restore-ci` | Restore database from CI dump |
| `t3 <overlay> db reset-passwords` | Reset all user passwords to a known value |

### `pr` — Merge request and ticket workflow

| Command | Purpose |
|---------|---------|
| `t3 <overlay> pr create` | Create a merge request for the ticket's branch |
| `t3 <overlay> pr check-gates` | Check whether session gates allow a phase transition |
| `t3 <overlay> pr fetch-issue` | Fetch issue details from the configured tracker |
| `t3 <overlay> pr detect-tenant` | Detect the current tenant variant |
| `t3 <overlay> pr post-evidence` | Post test evidence as an MR comment |

### `tasks` — Async task queue

| Command | Purpose |
|---------|---------|
| `t3 <overlay> tasks claim` | Claim the next available task |
| `t3 <overlay> tasks work-next-sdk` | Claim and execute a headless task |
| `t3 <overlay> tasks work-next-user-input` | Claim and execute a user input task |

### `followup` — Follow-up snapshots

| Command | Purpose |
|---------|---------|
| `t3 <overlay> followup refresh` | Return counts of tickets and tasks |
| `t3 <overlay> followup sync` | Synchronize followup data from MRs |
| `t3 <overlay> followup remind` | Return list of pending user input tasks |
