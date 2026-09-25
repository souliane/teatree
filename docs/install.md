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
uv tool install --from git+https://github.com/souliane/teatree.git teatree \
  --overrides https://raw.githubusercontent.com/souliane/teatree/main/uv-overrides.txt   # global `t3` binary
apm install -g souliane/teatree   # skills + companion deps
t3 setup                          # links plugin, syncs skills, migrates self-DB
```

### One portable plugin, two interactive harnesses

TeaTree ships one portable plugin payload: the shared `skills/` tree and
`.mcp.json`, plus thin Claude Code and Codex manifests/hook adapters. `t3 setup`
registers that same checkout with both runtimes. It does not maintain two copies
of the workflow prose.

The Claude plugin is not an authentication or SDK requirement. Historically it
was the native package that made namespaced `t3:*` skills, `t3 mcp serve`, and
TeaTree lifecycle hooks discoverable in a normal interactive Claude Code
session. Codex needs a native registration for the same interactive experience,
so the portable package exposes Codex's local-marketplace manifest as well.
Hooks are mapped only where the two runtimes have equivalent events; unsupported
Claude-only events are not simulated.

The headless `codex_app_server` harness is separate. It injects its prompt and
MCP configuration through the App Server protocol and manages its own ChatGPT
credential cache, so it does not need the interactive plugin for transport or
authentication. Install the plugin when people should be able to use ordinary
attended TeaTree sessions from Codex.

### Selective harness skills

`apm.yml` is the reviewed, read-only declaration of TeaTree's third-party skill
requirements. The dashboard never rewrites it. Setup enumerates those individual
requirements and any active overlay runtime demands, then calls the installed
exact `skills@1.7.0` CLI for only that set on Claude Code and Codex. It does not
install an entire skills repository merely because one skill is needed, and it
never downloads an implicit `npx` version at runtime.

Optional harness skills can be kept absent during headless setup with the
DB-backed `harness_skill_exclusions` list:

```sh
t3 <overlay> config_setting set harness_skill_exclusions \
  '["claude-code:unused-skill", "codex:unused-skill"]'
```

The Skills dashboard writes the same entries when an operator removes a skill.
TeaTree-owned prompt skills and independently required third-party skills remain
read-only; exclusions cannot disable context TeaTree needs for its workflow.
See [the dashboard Skills control plane](dashboard.md#skills-control-plane) for
inventory, provenance refresh, collision warnings, and removal behavior.

Headless deployments use `t3 setup --strict-agent-skills`. Strict setup removes
its previous readiness marker first and exits non-zero unless declared skills,
the inventory receipt, and both portable-plugin registrations converge. A normal
interactive `t3 setup` keeps its best-effort behavior. `--strict-agent-skills`
cannot be combined with `--skip-plugin`.

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

#### In a fork that vendors teatree core

A downstream fork keeps core at `<fork>/vendor/teatree` and edits it in place, so
the git boundary is the **fork root** while teatree's `pyproject.toml` sits one
level down. `t3 overlay install` recognises that layout and resolves the fork root
as the workspace — run it from anywhere in the fork, including inside
`vendor/teatree/`:

```sh
cd ~/workspace/<org>/<fork>
t3 overlay install <overlay-name>
```

A fork usually ships its overlay in the same repo (declared in the fork's own
`[project.entry-points."teatree.overlays"]`). There is then no separate checkout
to make a sibling worktree of — the overlay's source IS the workspace — so the
command installs the fork root itself, or reports the overlay as *already provided
by this workspace* when its package already imports from there (the normal state
after `uv tool install --editable vendor/teatree --with-editable .`). Because no
sibling worktree is created, running from the fork's main clone is allowed; the
main-clone refusal still applies wherever a sibling WOULD be created.

When `t3` runs through the containerized `deploy/t3` wrapper, the host cwd is
translated into container coordinates and passed as `TEATREE_INVOCATION_CWD`, so
"where you stand" survives the boundary. Standing outside the mounted tree leaves
it unset and the command resolves from the container's own cwd, as before.

The overlay's main clone path is recorded in the DB `overlays` registry row (one
JSON-dict `ConfigSetting` row keyed by overlay name):

```json
{"t3-acme": {"path": "~/workspace/t3-acme"}}
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
`xoxb-` (bot) and `xapp-` (app-level) tokens in `pass`, and records the
config in the DB `overlays` registry row. The bot needs Socket Mode enabled
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

Config lives in the DB `overlays` registry row (keyed by overlay name):

```json
{"<name>": {"messaging_backend": "slack", "slack_user_id": "U...", "slack_token_ref": "teatree/<name>/slack"}}
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
