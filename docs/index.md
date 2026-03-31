# TeaTree

A Django extension package that manages multi-repo development workflows with worktree isolation, port allocation, and AI agent orchestration.

## What it does

If your project spans several repos (backend, frontend, translations, configuration), starting work on a ticket means creating the same branch everywhere, setting up worktrees, remapping ports, provisioning databases, and wiring it all together. Teatree automates that.

The core idea: each workflow phase -- ticket intake, coding, testing, review, delivery -- is encoded as a skill file that an AI agent reads and follows. Skills are plain markdown and shell scripts. Any agent that can read files and run commands can use them.

## How it fits together

- **`teatree/`** -- the Django extension package. Models, management commands, overlay loader, views, and the `t3` CLI.
- **`skills/*/`** -- skill directories. Each teaches the agent one phase of the development lifecycle.
- **Overlay pattern** -- project-specific behaviour lives in a lightweight overlay package that subclasses `OverlayBase` and registers via a `teatree.overlays` entry point. Teatree stays generic; the overlay wires in your repos, services, and provisioning steps.

## Getting started

```bash
# Install teatree
pip install teatree   # or: uv add teatree

# Create an overlay package for your project
uv run t3 startoverlay my-overlay ~/workspace/my-overlay
```

Your overlay is a lightweight Python package with an `OverlayBase` subclass and a `teatree.overlays` entry point. Once installed alongside teatree, `t3 teatree dashboard` picks it up automatically -- no settings to configure.

## Further reading

- [Installation](install.md) -- setup and first project
- [Architecture](architecture.md) -- how the code is structured
- [CLI Reference](cli.md) -- the `t3` command and its subcommands
- [Overlay API](overlay-api.md) -- the contract between teatree and your project
- [Management Commands](management-commands.md) -- Django management commands exposed through `t3`
