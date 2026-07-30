# TeaTree

A personal code factory for multi-repo projects. Teatree drives a ticket from intake to merged MR by orchestrating worktrees, databases, ports, AI agents, and code-host sync across every repo the ticket touches.

## What it does

If your project spans several repos (backend, frontend, translations, configuration), starting work on a ticket means creating the same branch everywhere, setting up worktrees, remapping ports, provisioning databases, and wiring it all together. Teatree automates that — and then drives each ticket through its lifecycle: code, test, review, ship.

## Core concepts

Teatree coordinates work through **four state machines** — `Ticket`, `Worktree`, `Task`, `PullRequest` — each with typed transitions and tests. Agents read skills to do the *creative* work; the CLI owns the *mechanical* work.

The **CLI** (`t3 ...`) is the source of truth — everything else is a view on top. Two surfaces sit on it:

- **Statusline** — a 3-zone statusline (anchors, action needed, in flight), the always-on UI surface written to a file and read by the Claude Code statusline hook.
- **Claude plugin** — skills, hooks, and the autonomous per-domain loops (driven by the singleton `t3 worker`) that teach an agent how to drive the CLI.

## How it fits together

- **`src/teatree/`** -- the Django project. Models, management commands, overlay loader, code-host + messaging backends, the `/loop` and its scanners, and the `t3` CLI.
- **`skills/*/`** -- skill directories. Each teaches the agent one phase of the development lifecycle.
- **Overlay pattern** -- project-specific behaviour lives in a lightweight overlay package that subclasses `OverlayBase` and registers via a `teatree.overlays` entry point. Teatree stays generic; the overlay wires in your repos, services, code host, and messaging backend.

## Getting started

```bash
# Install teatree
git clone https://github.com/souliane/teatree && cd teatree && uv tool install --editable . && t3 setup

# Create an overlay package for your project
t3 startoverlay my-overlay ~/workspace/my-overlay
```

Your overlay is a lightweight Python package with an `OverlayBase` subclass and a `teatree.overlays` entry point. Once installed alongside teatree, the loops pick it up automatically — driven by the singleton `t3 worker`, or by a `/loop` in your interactive Claude Code session.

## Further reading

- [Installation](install.md) -- setup and first project
- [Architecture](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) -- the canonical architecture spec
- [CLI Reference](generated/cli-reference.md) -- the `t3` command and its subcommands
- [Overlay API](overlay-api.md) -- the contract between teatree and your project
- [Management Commands](generated/management-commands.md) -- Django management commands exposed through `t3`
