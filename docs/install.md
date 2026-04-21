# Installing TeaTree

## How it works

- **teatree** provides the `t3` CLI and the core framework
- **Overlays** (like `t3-acme`) are separate packages that depend on teatree
- Overlays register via Python entry points — `t3` discovers them automatically

## Install paths

### Use only (no source code)

Install teatree, or an overlay (which pulls teatree as a dependency):

```sh
uv pip install teatree           # teatree only
uv pip install t3-acme           # overlay (includes teatree)
t3 --help
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

When developing a teatree feature on a ticket branch and you need to drive it through an overlay in the browser:

```sh
cd ~/workspace/ac-teatree-123-my-branch/teatree     # teatree worktree
t3 overlay install <overlay-name>                    # e.g. t3-acme
t3 dashboard
```

This creates a sibling `git worktree` for the overlay (matching the teatree branch when it exists, otherwise the overlay's default branch) and installs it editable into the teatree worktree's `.venv`. The worktree's `t3` shadows the global install while you're inside it, so the dashboard and any agents use that branch's code.

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

These checks run automatically on `t3 doctor check` and as a Django
system check (warns on every `t3` invocation if misconfigured).
