# TeaTree

A personal code factory for multi-repo projects. Teatree drives a ticket from intake to merged MR by orchestrating worktrees, databases, ports, AI agents, and code-host sync across every repo the ticket touches.

## What it does

If your project spans several repos (backend, frontend, translations, configuration), starting work on a ticket means creating the same branch everywhere, setting up worktrees, remapping ports, provisioning databases, and wiring it all together. Teatree automates that — and then drives each ticket through its lifecycle: code, test, review, ship.

## Core concepts

Teatree coordinates work through **four state machines** — `Ticket`, `Worktree`, `Task`, `PullRequest` — each with typed transitions and tests. Agents read skills to do the *creative* work; the CLI owns the *mechanical* work.

Two surfaces sit on top:

- **CLI** (`t3 ...`) — the source of truth. Everything else is a view on top.
- **Statusline** — the loop renders a 3-zone statusline (anchors, action needed, in flight) that is the only persistent UI surface.
- **Claude plugin** — skills, hooks, and a fat `/loop` that teach an agent how to drive the CLI.

## How it fits together

- **`teatree/`** -- the Django project. Models, management commands, overlay loader, code-host + messaging backends, the `/loop` and its scanners, and the `t3` CLI.
- **`skills/*/`** -- skill directories. Each teaches the agent one phase of the development lifecycle.
- **Overlay pattern** -- project-specific behaviour lives in a lightweight overlay package that subclasses `OverlayBase` and registers via a `teatree.overlays` entry point. Teatree stays generic; the overlay wires in your repos, services, code host, and messaging backend.

## Getting started

```bash
# Install teatree
pip install teatree   # or: uv add teatree

# Create an overlay package for your project
t3 startoverlay my-overlay ~/workspace/my-overlay
```

Your overlay is a lightweight Python package with an `OverlayBase` subclass and a `teatree.overlays` entry point. Once installed alongside teatree, `/loop` (in your interactive Claude Code session) picks it up automatically.

## Further reading

- [Installation](install.md) -- setup and first project
- [Architecture](../BLUEPRINT.md) -- the canonical architecture spec
- [CLI Reference](cli.md) -- the `t3` command and its subcommands
- [Overlay API](overlay-api.md) -- the contract between teatree and your project
- [Management Commands](management-commands.md) -- Django management commands exposed through `t3`
