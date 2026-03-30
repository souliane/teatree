# Architecture

## Three-tier command split

Teatree splits its commands into three layers:

1. **Management commands** (`teatree/core/management/commands/`) -- Django management commands that touch the database. These handle lifecycle transitions, workspace operations, DB refresh, MR validation, task queue processing, and follow-up. They use [django-typer](https://github.com/bckohan/django-typer) for a typed CLI interface.

2. **CLI commands** (`teatree/cli.py`) -- the `t3` entry point. Commands that don't need Django (like `startproject`, `ci`, `doctor`, `review-request`) live here as plain Typer groups. Commands that need the database are bridged to management commands after Django bootstrap.

3. **Internal utilities** (`teatree/utils/`) -- helper modules for git operations, postgres interaction, and other low-level work. These are not exposed as commands; they're used by the layers above.

## Models

Five models in `teatree/core/models.py` track the state of ongoing work:

| Model | Purpose |
|-------|---------|
| **Ticket** | Tracks a unit of work through its lifecycle (not_started -> scoped -> started -> coded -> tested -> reviewed -> shipped -> merged -> delivered). Uses django-fsm for state transitions. |
| **Worktree** | Represents one repo checkout within a ticket's workspace. Tracks allocated ports, DB name, and its own lifecycle (created -> provisioned -> services_up -> ready). |
| **Session** | An agent session working on a ticket. Records which phases have been visited and enforces quality gates (e.g., you can't ship without testing). |
| **Task** | A unit of work that can be claimed by an SDK worker or routed to a human for input. Supports lease-based claiming with heartbeats. |
| **TaskAttempt** | Records each execution attempt of a task, with exit code and artifact path. |

## The overlay pattern

Teatree is generic -- it doesn't know your repos, CI setup, or environment. Project-specific behaviour lives in a separate Django project (the "host project") that you generate with `t3 startproject`.

The host project defines one overlay class that subclasses `OverlayBase` and implements the hooks teatree needs:

```python
# In your host project
TEATREE_OVERLAY_CLASS = "myoverlay.overlay.MyOverlay"
```

The overlay loader (`teatree/core/overlay_loader.py`) imports and caches this class at runtime. Management commands call overlay hooks when they need project-specific information -- which repos to manage, how to provision a worktree, what services to start, how to validate an MR.

See [Overlay API](overlay-api.md) for the full contract.

## Package layout

```
teatree/
  core/           # Models, managers, selectors, views, management commands, templates
  agents/         # Runtime adapters for AI agent platforms
  backends/       # Integration backends (GitLab, Slack, Notion)
  scaffold/       # Templates for `t3 startproject`
  utils/          # Git, postgres, and other internal helpers
  cli.py          # The `t3` entry point
  skill_map.py    # Skill metadata registry
```
