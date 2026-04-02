---
name: teatree
description: TeaTree agent lifecycle platform — installation, configuration, lifecycle phases, overlay concept, CLI reference, and skill loading. Use when working on teatree itself or when understanding how teatree orchestrates agent workflows.
metadata:
  version: 0.0.1
triggers:
  priority: 90
  keywords:
    - '\b(teatree|t3 )\b'
    - '\b(lifecycle|overlay|worktree|provision|headless)\b'
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
3. **Test the full flow** — if the change involves task execution, create a task and verify the worker picks it up. Don't declare "auto-start works" without observing a task transition from PENDING to CLAIMED.
4. **Check overlay resolution from worktrees** — `discover_active_overlay()` uses cwd-based discovery. Worktree directory names don't match overlay names. Always test from a worktree path, not the main clone.

**Known pitfall:** `discover_active_overlay()` returns the directory name when `manage.py` is found via cwd walk. In worktrees, this gives names like `move-dashboard-to-general-cli` instead of `t3-teatree`. The `_resolve_overlay_for_server()` function in `cli/__init__.py` works around this by preferring entry-point overlays.

## Configuration

`~/.teatree` sourced by hooks:

```bash
T3_REPO="$HOME/workspace/souliane/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # never auto-push retro commits
T3_PRIVACY=strict                            # block commits with PII
```
