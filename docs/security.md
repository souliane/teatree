# Security: subprocess usage and overlay trust

Teatree executes subprocess calls on behalf of overlay hooks.
This document records which calls accept overlay-supplied strings,
the trust boundary that makes this acceptable, and the contract
overlay authors must honour.

## Trust boundary

Overlays are **trusted, locally-installed Python packages**.
A user installs an overlay the same way they install any Python
dependency — a local editable install
(`uv tool install --editable . --overrides uv-overrides.txt`).
Code that runs inside an overlay has the same privilege level as the
user's shell.

Teatree therefore does **not** sanitize, escape, or sandbox strings
returned by overlay hooks before passing them to `subprocess`.

## Subprocess calls that consume overlay-supplied values

### `shell=True` — tool commands

**File:** `src/teatree/core/management/commands/tool.py`

`OverlayMetadata.get_tool_commands()` returns a list of `ToolCommand`
dicts.  Each dict may contain a `command` string.  The `tool run`
management command passes that string directly to
`subprocess.run(mgmt_cmd, shell=True, ...)`.

User-supplied extra arguments are appended via `shlex.join()`, so
those are safely quoted.  The base command string itself comes from
the overlay and is executed as-is.

### `shell=False` with overlay-provided argument lists

The following hooks return `list[str]` values that teatree passes as
the `args` parameter to `subprocess.run()` or `subprocess.Popen()`
(no shell interpretation):

| Hook | Return type | Consuming command |
|---|---|---|
| `OverlayRuntime.run_commands()` | `dict[str, list[str]]` | `run backend`, `run build-frontend`, `run tests`, `worktree start` |
| `OverlayRuntime.test_command()` | `list[str]` | `run tests` |
| `OverlayRuntime.lint_command()` | `list[str]` | `run lint` |
| `OverlayProvisioning.services_config()` | `dict[str, ServiceSpec]` | `run backend`, `worktree start` (reads `start_command`) |
| `OverlayBase.get_provision_steps()` | `list[ProvisionStep]` | `worktree provision` (calls `step.callable()`) |
| `OverlayProvisioning.post_db_steps()` | `list[ProvisionStep]` | `worktree provision` |
| `OverlayRuntime.pre_run_steps()` | `list[ProvisionStep]` | `worktree start` / `worktree provision` |
| `OverlayProvisioning.cleanup_steps()` | `list[ProvisionStep]` | `workspace clean-all` |
| `OverlayProvisioning.reset_passwords_command()` | `ProvisionStep \| None` | `worktree provision` |
| `OverlayProvisioning.env_extra()` | `dict[str, str]` | Injected into subprocess `env` for run/lifecycle commands |
| `OverlayProvisioning.envrc_lines()` | `list[str]` | Written to `.envrc` in the worktree directory |

`ProvisionStep.callable` is an arbitrary `Callable[[], None]` — the
overlay can do anything it wants inside that callback.

### Environment variables

`OverlayProvisioning.env_extra()` returns a dict merged into `os.environ`
before spawning service processes.  A malicious overlay could inject
`LD_PRELOAD`, `PATH`, or similar variables.  This is acceptable
because the overlay already has full code-execution capability through
provision steps and tool commands.

## The contract

Overlay authors **must**:

1. Return well-formed commands.  `get_tool_commands()` strings are
   passed to `shell=True` — they must not contain unintended shell
   metacharacters.
2. Return safe argument lists.  Hooks that return `list[str]` values
   should contain only literal arguments, not unsanitised user input.
3. Treat `ProvisionStep.callable` as privileged code.  It runs with
   the user's full permissions and no sandbox.
4. Keep `overlay.provisioning.env_extra()` values to what the service actually needs.
   Do not inject variables that alter the behaviour of unrelated
   processes.

Teatree's side of the contract: it will **never** sanitize or modify
the values returned by these hooks.  What the overlay returns is what
gets executed.

## Recommendations for overlay authors

- Prefer `list[str]` commands over shell strings wherever possible.
  The `overlay.runtime.run_commands()` and `overlay.runtime.test_command()` hooks already
  expect lists.
- When a tool command must use `shell=True` (pipelines, redirects,
  globbing), keep the command string as a static constant rather than
  assembling it from variables at runtime.
- Never incorporate end-user input (ticket descriptions, branch
  names, MR titles) into command strings without explicit escaping
  via `shlex.quote()`.
- If a provision step needs to run a subprocess internally, use
  `subprocess.run([...], check=True)` (list form, no shell) whenever
  practical.
