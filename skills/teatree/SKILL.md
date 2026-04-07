---
name: teatree
description: TeaTree agent lifecycle platform — installation, configuration, lifecycle phases, overlay concept, CLI reference, and skill loading. Use when working on teatree itself or when understanding how teatree orchestrates agent workflows.
metadata:
  version: 0.0.1
triggers:
  priority: 90
  keywords:
    - '\b(teatree|t3)\b'
    - '\b(lifecycle|overlay|worktree|provision|headless)\b'
    - '\b(plan(ning)?|prioriti[zs]e|backlog|project board|sprint|roadmap)\b'
  exclude: '\b(t3:code|t3:test|t3:ship|t3:debug|t3:review)\b'
search_hints:
  - teatree
  - lifecycle
  - overlay
  - worktree
  - provision
  - headless
  - skill loading
  - agent workflow
  - planning
  - prioritize
  - backlog
  - project board
---

# TeaTree — Agent Lifecycle Platform

TeaTree is a Django project that orchestrates agent workflows through lifecycle phases. Overlays are lightweight Python packages that extend it for specific projects.

## Architecture

- **TeaTree IS the Django project.** `pip install teatree` works standalone.
- **Overlays** register via `teatree.overlays` entry points and provide project-specific configuration.
- **Skills** live in `skills/` and are loaded by the agent's skill system.
- **Hooks** in `hooks/scripts/` run on agent lifecycle events (e.g., prompt submit, pre/post tool use).

## Lifecycle Phases

```
ticket → code → test → review → ship → review-request
```

Each phase maps to a skill (`t3:ticket`, `t3:code`, etc.). The `Session` model tracks visited phases and enforces quality gates (e.g., can't ship without testing).

## CLI Reference

```bash
t3 dashboard                # Start dashboard + background worker (top-level)
t3 <overlay> resetdb        # Drop and recreate the SQLite database
t3 lifecycle setup          # Provision worktree (ports, DB, overlay steps)
t3 lifecycle start          # Start dev servers
t3 lifecycle status         # Show worktree state
t3 lifecycle teardown       # Stop services, clean up
t3 tasks work-next-sdk      # Claim and execute next headless task
t3 tasks work-next-user-input  # Claim and launch next interactive task
t3 followup sync            # Daily ticket/MR sync
```

## Key Models

- **Ticket** — issue URL, overlay, variant, repos
- **Worktree** — repo path, branch, ports, state (FSM: created → provisioned → services_up → ready)
- **Session** — agent session with visited phases, repos modified/tested
- **Task** — claimable work unit with lease, heartbeat, parent chain
- **TaskAttempt** — execution result with exit code, structured output

## Overlay API

Overlays subclass `OverlayBase` and override methods:

- `get_repos()` — repo list for worktree creation
- `get_provision_steps(worktree)` — setup steps (migrations, fixtures)
- `get_run_commands(worktree)` — dev server commands
- `get_db_import_strategy(worktree)` — DSLR/dump import config
- `get_services_config(worktree)` — Docker services

## Skill Loading

TeaTree's UserPromptSubmit hook detects intent from user prompts using `triggers:` patterns in skill frontmatter. The hook suggests loading the matching skill. A PreToolUse hook blocks Bash/Edit/Write until suggested skills are loaded.

The `SkillLoadingPolicy` class resolves which skills to load based on intent, overlay, and current phase. For headless tasks, `search_hints` in frontmatter provide keyword matching.

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When adding or changing hooks, only modify `hooks.json` in this repo.

**Known failure (2026-04-02):** PR #109 moved hooks from `settings.json` to plugin `hooks.json` but didn't remove the old ones. This caused double hook execution on every tool call, accelerating context consumption and triggering aggressive microcompaction. Prevention: when migrating hooks to the plugin, always remove the `settings.json` equivalents in the same change.

## Dogfooding Checklist (Non-Negotiable for CLI/Server Changes)

When modifying CLI commands, dashboard views, or server startup:

1. **Run the command yourself** — don't rely on unit tests alone. `uv run t3 <command>` from a worktree (not the main clone) to catch cwd-dependent bugs.
2. **Verify HTTP 200** — for dashboard/server changes: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:<port>/` must return 200.
3. **Run E2E tests** — dashboard changes require Playwright E2E tests in `e2e/test_dashboard.py`. Start the server with `t3 dashboard` and run: `DJANGO_SETTINGS_MODULE=e2e.settings uv run --group e2e pytest e2e/ --ds e2e.settings --no-cov -v`.
4. **Test the full flow** — if the change involves task execution, create a task and verify the worker picks it up. Don't declare "auto-start works" without observing a task transition from PENDING to CLAIMED.
5. **Check overlay resolution from worktrees** — `discover_active_overlay()` uses cwd-based discovery. Worktree directory names don't match overlay names. Always test from a worktree path, not the main clone.

**Known pitfall:** `discover_active_overlay()` returns the directory name when `manage.py` is found via cwd walk. In worktrees, this gives names like `move-dashboard-to-general-cli` instead of `t3-teatree`. The `_resolve_overlay_for_server()` function in `cli/__init__.py` works around this by preferring entry-point overlays.

**Known pitfall:** `uv run` rebuilds editable installs and can silently revert uncommitted source edits. See `workspace/references/troubleshooting.md` § "uv run Silently Reverts Edits". Commit changes before running `uv run pytest`, or verify file content after test runs.

## Planning & Backlog Prioritization

When the user asks for planning, prioritization, or backlog management, follow this workflow. The goal is to keep the GitHub Projects v2 board as the single source of truth for what to work on next.

### Prerequisites

- The overlay must have `github_owner` and `github_project_number` configured in its `overlay_settings.py`. These are user settings — never hardcode project URLs.
- The `gh` CLI must have the `project` scope. If missing: `gh auth refresh -s read:project -s project`.

### 1. Sync Issues to the Project Board

**Auto-add all repo issues that aren't on the board yet:**

```bash
# List all open issues in the repo
gh issue list --repo <owner>/<repo> --state open --json number,url --limit 200

# List items already on the board
gh project item-list <project_number> --owner <owner> --format json

# For each issue NOT on the board:
gh project item-add <project_number> --owner <owner> --url <issue_url>
```

Do this for ALL repos the overlay manages (check `get_repos()` or the repo's GitHub org). Every open issue must be on the board — no orphans.

### 2. Present the Backlog

Fetch all board items and present them grouped by status column:

| Priority | # | Title | Labels | Status | Iteration |
|----------|---|-------|--------|--------|-----------|
| 1 | #97 | Shared Docker images | architecture | Todo | — |
| 2 | #167 | Dockerize t3 itself | enhancement | Todo | — |
| ... | | | | | |

Sort within each status by current board position (the board's drag order is authoritative).

### 3. Help Prioritize

Guide the user through prioritization by asking about each unordered item:

- **Dependencies**: "Does #X block #Y? Should #X come first?"
- **Impact vs effort**: "This looks high-impact/low-effort — move it up?"
- **Grouping**: "These 3 issues are related — tackle them together?"

Ask **one question at a time** using `AskUserQuestion`. Never dump a priority matrix — walk through it interactively.

After the user decides, reorder the board:

```bash
# Move an item to a specific position using the GraphQL API
gh api graphql -f query='
  mutation {
    updateProjectV2ItemPosition(input: {
      projectId: "<project_node_id>"
      itemId: "<item_node_id>"
      afterId: "<after_item_node_id>"
    }) { item { id } }
  }'
```

### 4. Update Status Columns

If the user wants to move items between columns (Todo → In Progress, etc.):

```bash
# Get the field ID for Status and the option IDs
gh project field-list <project_number> --owner <owner> --format json

# Update an item's status
gh project item-edit --project-id <project_id> --id <item_id> --field-id <status_field_id> --single-select-option-id <option_id>
```

### 5. Iteration (Optional)

The board may have an "Iteration" field for sprint-like grouping. If the user asks about sprints or iterations, use it. Otherwise, don't force it — priority order within "Todo" is sufficient.

### 6. What to Tackle Next

After prioritization, the agent knows the backlog order. When the user asks "what's next?" or starts a new session:

1. Fetch the board (`fetch_project_items` or `gh project item-list`).
2. The first item in "Todo" (by board position) is the next ticket.
3. Suggest it: "Next up: #X — *title*. Load `/t3:ticket` to start?"

This replaces guessing or asking the user what to work on — the board is the queue.

## Configuration

`~/.teatree` sourced by hooks:

```bash
T3_REPO="$HOME/workspace/souliane/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # never auto-push retro commits
T3_PRIVACY=strict                            # block commits with PII
```
