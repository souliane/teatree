# Installing TeaTree

## How it works

- **teatree** provides the `t3` CLI and the core framework
- **Overlays** (like `t3-acme`) are separate packages that depend on teatree
- Overlays register via Python entry points — `t3` discovers them automatically

## Install paths

### Use only (no source code)

Teatree is not on PyPI. Install the `t3` CLI straight from the repo — no clone
needed:

```sh
uv tool install --from git+https://github.com/souliane/teatree.git teatree   # global `t3` binary
apm install -g souliane/teatree   # skills + companion deps
t3 setup                          # links plugin, syncs skills, migrates self-DB
```

### Work locally on an overlay

Clone the overlay and install dependencies:

```sh
git clone <overlay-repo>
cd t3-acme
uv sync                          # installs teatree as a regular dependency
```

This is enough to edit overlay code and run tests (`uv run pytest`).
But it's **not enough for full agent DX** — the `t3` CLI lives in the
venv's `site-packages`, not in your checkout. If an agent modifies
overlay files, those changes land in your working tree. But if it
modifies teatree files, those changes go to a read-only installed copy
and are lost on next sync.

For full round-trip development (where the agent can edit overlay code
and you see the changes immediately):

```sh
uv pip install -e .              # install overlay itself as editable
t3 doctor check                    # verify editable status
```

### Contribute to teatree

Clone teatree and install it as editable in the overlay:

```sh
git clone <teatree-repo>         # ~/workspace/teatree
cd ~/workspace/t3-acme
```

In the overlay's `pyproject.toml`:

```toml
[tool.uv.sources]
teatree = { path = "../teatree", editable = true }
```

Then:

```sh
uv sync                          # teatree installed as editable
t3 doctor check                    # verify both are editable
```

Now changes to teatree source files are picked up immediately.

#### CI compatibility

The local editable path won't exist in CI. Add this step **before** `uv sync`
in your CI workflow to override the source:

```yaml
- run: uv add teatree --no-editable --git https://github.com/souliane/teatree.git
```

This replaces the local path with a git install for that CI run only.
The committed `pyproject.toml` is unchanged.

When teatree is published to an index, replace the override with:

```yaml
env:
  UV_NO_SOURCES_PACKAGE: teatree
```

This tells uv to ignore `[tool.uv.sources]` for teatree and resolve
from the index instead.

### Contribute to both

Same as above — both the overlay and teatree are editable:

```sh
cd ~/workspace/t3-acme
uv pip install -e .              # overlay editable
# pyproject.toml already points teatree to editable path
uv sync
t3 doctor check                    # both show as editable
```

### Dogfood a teatree branch with an overlay

When developing a teatree feature on a ticket branch and you need to drive it through an overlay:

```sh
cd ~/workspace/ac-teatree-123-my-branch/teatree     # teatree worktree
t3 overlay install <overlay-name>                    # e.g. t3-acme
```

This creates a sibling `git worktree` for the overlay (matching the teatree branch when it exists, otherwise the overlay's default branch) and installs it editable into the teatree worktree's `.venv`. The worktree's `t3` shadows the global install while you're inside it, so any agents use that branch's code.

Configure the overlay's main clone path in `~/.teatree.toml`:

```toml
[overlays.t3-acme]
path = "~/workspace/t3-acme"
```

Undo and inspect:

```sh
t3 overlay status
t3 overlay uninstall <overlay-name>
```

The main clone (detected via a real `.git` directory) refuses `install` — use this in worktrees only. Tracked overlays persist in `.t3.local.json` (gitignored).

## Slack integration (optional)

Each overlay can have its own Slack bot for bidirectional messaging
(question mirroring, DM monitoring, mention scanning). Setup per overlay:

```sh
t3 setup slack-bot --overlay <name>
```

This walks through Slack app creation, generates a manifest, stores
`xoxb-` (bot) and `xapp-` (app-level) tokens in `pass`, and writes the
config to `~/.teatree.toml`. The bot needs Socket Mode enabled
(`connections:write` scope on the app-level token).

Start the event listener (runs in foreground, one WebSocket per overlay):

```sh
t3 slack listen                    # all slack-enabled overlays
t3 slack listen --overlay <name>   # single overlay
t3 slack status                    # check if the listener is running
```

The listener writes inbound events to a JSONL queue. The drain-queue loop
(`t3 loop drain-queue run`) drains the queue and surfaces mentions/DMs in the
statusline. The Claude Code hook mirrors `AskUserQuestion` prompts to
Slack DM so you can answer from your phone.

Config lives in `~/.teatree.toml`:

```toml
[overlays.<name>]
messaging_backend = "slack"
slack_user_id = "U..."          # your Slack member ID
slack_token_ref = "teatree/<name>/slack"  # pass entry prefix
```

## Overlay discovery

Overlays register via standard Python entry points in `pyproject.toml`:

```toml
[project.entry-points."teatree.overlays"]
my-overlay = "my_package.settings"
```

The `t3` CLI uses this to auto-detect which Django settings module to use.

### Settings resolution priority

1. `--settings` CLI flag
2. `manage.py` in current directory ancestors (developer in project dir)
3. Single installed overlay entry point (end-user install)
4. `teatree.dev_settings` fallback (teatree contributor, no overlay)

## Sanity checks

`t3 doctor check` verifies that editable status matches your intent:

- **Contributing to teatree?** It must be editable. Otherwise your
  changes go to a build artifact and are silently lost on next sync.
- **Not contributing to teatree?** It should be a normal install.
  Otherwise you risk accidentally modifying framework code.
- Same rules apply to the overlay package.

It also **FAILs when the installed `t3` is anchored to a git worktree**
instead of the primary clone (a stale editable `.pth`): the worktree-resident
code auto-isolates onto a per-worktree DB while the loop and canonical state
live in the canonical DB, so work silently diverges. The fix is to re-anchor
the editable install at the primary clone (re-run `t3 setup` from it).

These checks run automatically on `t3 doctor check` and as a Django
system check (warns on every `t3` invocation if misconfigured).
