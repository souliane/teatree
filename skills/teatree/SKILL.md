---
name: teatree
description: TeaTree agent lifecycle platform — core architecture, lifecycle phases, CLI reference, overlay API, skill loading, and plugin hooks. Use when working on teatree itself or when understanding how teatree orchestrates agent workflows. Mode-specific skills (dogfooding, planning, batch, bug hunt) are separate — see the "Related skills" section below.
metadata:
  version: 0.0.1
triggers:
  priority: 90
  keywords:
    - '\b(teatree|t3)\b'
    - '\b(overlay|provision|headless)\b'
  exclude: '\b(t3:code|t3:test|t3:ship|t3:debug|t3:review|batch mode|bug hunt|dogfood|plan(ning)?|backlog|prioriti[zs]e)\b'
search_hints:
  - teatree
  - lifecycle
  - overlay
  - provision
  - headless
  - skill loading
  - agent workflow
---

# TeaTree — Agent Lifecycle Platform

TeaTree is a personal code factory for multi-repo projects — it turns a ticket URL into a merged pull request by driving AI agents through lifecycle phases. Under the hood it's a Django project; overlays are lightweight Python packages that extend it for specific projects.

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
- `get_visual_qa_targets(changed_files)` — URL paths the pre-push browser sanity gate should load (default: `[]` — opt in by mapping diff paths to URLs)

## Skill Loading

TeaTree's UserPromptSubmit hook detects intent from user prompts using `triggers:` patterns in skill frontmatter. The hook suggests loading the matching skill. A PreToolUse hook blocks Bash/Edit/Write until suggested skills are loaded.

The `SkillLoadingPolicy` class resolves which skills to load based on intent, overlay, and current phase. For headless tasks, `search_hints` in frontmatter provide keyword matching.

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When adding or changing hooks, only modify `hooks.json` in this repo.

**Known failure (2026-04-02):** PR #109 moved hooks from `settings.json` to plugin `hooks.json` but didn't remove the old ones. This caused double hook execution on every tool call, accelerating context consumption and triggering aggressive microcompaction. Prevention: when migrating hooks to the plugin, always remove the `settings.json` equivalents in the same change.

## Configuration

`~/.teatree` sourced by hooks:

```bash
T3_REPO="$HOME/workspace/souliane/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # gate pushes behind an explicit prompt
T3_AUTO_PUSH_FORK=false                      # auto-push to fork when T3_PUSH=true and origin ≠ T3_UPSTREAM
T3_AUTO_SHIP=false                           # when true, shipping tasks are headless; default gates push on user approval
T3_PRIVACY=strict                            # block commits with PII
```

## Related Skills

This skill holds the core. Load the mode-specific skill for the task in hand — none `require:` this one, to keep per-invocation context small.

| Skill | When to load |
|-------|--------------|
| `/teatree-dogfood` | Validating a CLI, dashboard, or server startup change |
| `/teatree-plan` | Prioritizing the backlog via the GitHub Projects v2 board |
| `/teatree-batch` | Working the prioritized backlog unattended, one ticket at a time |
| `/teatree-bughunt` | Self-QA on the dashboard — find, file, and fix bugs in one session |
