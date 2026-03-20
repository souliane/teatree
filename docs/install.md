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
uv run t3 doctor check           # verify editable status
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
uv run t3 doctor check           # verify both are editable
```

Now changes to teatree source files are picked up immediately.

### Contribute to both

Same as above — both the overlay and teatree are editable:

```sh
cd ~/workspace/t3-acme
uv pip install -e .              # overlay editable
# pyproject.toml already points teatree to editable path
uv sync
uv run t3 doctor check           # both show as editable
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
4. `teetree.dev_settings` fallback (teatree contributor, no overlay)

## Sanity checks

`t3 doctor check` verifies that editable status matches your intent:

- **Contributing to teatree?** It must be editable. Otherwise your
  changes go to a build artifact and are silently lost on next sync.
- **Not contributing to teatree?** It should be a normal install.
  Otherwise you risk accidentally modifying framework code.
- Same rules apply to the overlay package.

These checks run automatically on `t3 doctor check` and as a Django
system check (warns on every `t3` invocation if misconfigured).
