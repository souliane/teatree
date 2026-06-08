# Management Commands

Teatree's Django management commands handle all database-touching operations. They use [django-typer](https://github.com/bckohan/django-typer) so each command exposes typed subcommands.

These are also accessible through the `t3` CLI (see [CLI Reference](generated/cli-reference.md)), but you can call them directly with `manage.py` if you prefer.

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
| `record-review-skill-run` | `ticket_id`, `skill` | string | Stamps `review_skill_run` evidence (skill + UTC ISO timestamp) so the reviewing-phase gate (#1539) accepts the attestation |
| `record-anti-vacuity` | `ticket_id`, `--head-sha`, `--ac-coverage`, `--proven-test`, `--no-new-tests` | string | Stamps the SHA-bound `anti_vacuity_attestation` so the review-request/merge anti-vacuity gate (#1829) accepts the transition |
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
| `migrate` | — | string | Applies pending migrations to the runtime self-DB in-process (non-destructive self-rescue for a stale control DB; the always-available unblock when the merge path refuses on unapplied migrations) |
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
| `work-next-sdk` | `claimed_by` | dict or None | Claims and completes the next headless task using the configured runtime; refuses loop-dispatched phases (records a `routing_error`) unless `LOOP_ALLOW_HEADLESS_DISPATCH` is set |
| `work-next-user-input` | `claimed_by` | dict or None | Claims and completes the next user-input task |
| `cancel` | `task_id`, `--confirm` | None | Cancels a pending or claimed task. Requires `--confirm` for claimed tasks. |
| `list` | `--status`, `--execution-target` | list of dicts | Lists tasks, optionally filtered by status and/or execution target |

## `queue`

Background-task DB queue (`DBTaskResult`). The drain rides the loop tick (`teatree.loop.queue_drain`); this surface is for inspection and the one-off stale-job retirement.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `status` | -- | None | Prints the queue breakdown by status and READY jobs by task name (read-only) |
| `expire-stale` | `--hours`, `--dry-run` | None | Retires READY jobs older than the threshold (default `T3_QUEUE_STALE_HOURS`, 24h) to `FAILED` so a drainer never runs them |

## `followup`

Monitoring and reminders.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `refresh` | -- | dict | Returns counts of tickets, tasks, and open tasks |
| `sync` | -- | dict | Syncs follow-up data: discovered MRs, created/updated tickets, errors |
| `discover-mrs` | -- | dict | Lists the user's open, non-draft PRs/MRs (`repo`, `iid`, `title`, `url`) awaiting a review request |
| `remind` | -- | list of IDs | Returns IDs of pending interactive tasks |

## `standup`

Auto-generated daily update (read-only — no state mutation). Derived
entirely from `TicketTransition` + `TaskAttempt` + per-worktree `git log`.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `generate` | `--days`, `--since` | dict | Cross-references phase changes and agent runs in the window; returns `{since, yesterday, blockers, markdown}` |
| `stale` | `--days` | list of dicts | Tickets with no `TaskAttempt`/`TicketTransition` activity past the threshold (same query as the `stale_tickets` scanner) |

## `mr_reminder`

Cross-repo "my open MRs" Slack reminder. Lists every open MR/PR the user
authors across all repos one code-host token can see, routes each to a
Slack channel via the `[mr_reminder]` repo→channel map, and assembles one
message per channel. Assembly + routing are pure (`teatree.core.mr_reminder`);
the per-channel post routes through the on-behalf egress chokepoint
(`OnBehalfSlackEgress.post`) — a reminder channel is a colleague surface, so it is
gated + audited like any on-behalf post.

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `preview` | `--header` | dict | Assembles the per-channel reminder read-only (no Slack post); returns `{total, channels:[{channel, count, text}], unrouted}` |
| `send` | `--header` | dict | Posts one message per routed channel; returns `{total, posted, failed, unrouted}` (exit 1 if any post fails) |

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
| `post-evidence` | `--manifest`, `--ticket`, `--title`, `--mrs` | dict | Posts/updates the ticket's ONE structured evidence note (not the MR) from a manifest: a side-by-side Dev\|Local test-plan table per workflow, multi-repo MR links, per-env commit provenance, and a dev-gap reconciliation line. A hidden machine-readable state blob keyed on the ticket makes re-runs merge each env in place (dev-only run freezes local, and vice versa). Media embeds the relative `/uploads/<secret>/<file>` reference GitLab claims on save so it renders. |

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
| `handle` | `output_dir` (default `docs/generated`) | string | Generates the overlay extension-points and skill-delegation docs. The CLI reference is generated separately by the `generate-cli-reference` pre-commit hook (`scripts/hooks/generate_cli_reference.py`), not this command. |

## `generate_overlay_docs`

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output_dir` | string | Generates deterministic overlay extension-point documentation (JSON + Markdown) |

## `generate_skill_docs`

| Subcommand | Arguments | Returns | Description |
|------------|-----------|---------|-------------|
| `handle` | `output_dir`, `skill_map` | string | Generates deterministic skill delegation documentation (JSON + Markdown) |
