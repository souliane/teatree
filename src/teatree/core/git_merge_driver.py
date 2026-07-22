"""Register the ``generated`` git merge driver in every checkout (#3582).

``.gitattributes`` marks the generated docs ``merge=generated``, but that only
names a driver — the driver's *command* lives in per-clone ``.git/config`` and is
never committed. Without it, a ``merge=generated`` path silently falls back to a
textual 3-way merge (conflict markers on every CLI-touching PR). This installs
the ``git config merge.generated.driver`` value so a checkout's merges resolve by
regeneration.

Django-free (stdlib plus :mod:`teatree.utils.run`) so ``t3 setup`` can call it
before ``ensure_django``. Idempotent: ``git config`` overwrites, so a re-run
rewrites the identical value. Worktrees share the main clone's ``.git/config``,
so registering once per clone covers all its worktrees — but a per-worktree
re-run is a harmless no-op that keeps a hand-created worktree covered too.
"""

from typing import TYPE_CHECKING

from teatree.utils.run import CommandFailedError, run_allowed_to_fail

if TYPE_CHECKING:
    from pathlib import Path

_DRIVER_NAME = "regenerate generated docs on conflict (souliane/teatree#3582)"
# ``uv run python`` supplies the venv interpreter with teatree + Django installed;
# git runs the driver with cwd at the worktree root, where pyproject.toml lives.
_DRIVER_COMMAND = "uv run python scripts/hooks/git_merge_generated.py %O %A %B %P"


def merge_driver_command() -> str:
    """The ``merge.generated.driver`` value teatree registers."""
    return _DRIVER_COMMAND


def install_merge_driver(checkout: "Path") -> str:
    """Register the ``generated`` merge driver in *checkout*; return a status line.

    Never raises — a git failure degrades to a ``WARN`` line so setup and
    provisioning continue (the driver is an optimization, not a correctness gate;
    the CI sync checks remain the backstop).
    """
    try:
        run_allowed_to_fail(
            ["git", "-C", str(checkout), "config", "merge.generated.name", _DRIVER_NAME],
        )
        run_allowed_to_fail(
            ["git", "-C", str(checkout), "config", "merge.generated.driver", _DRIVER_COMMAND],
        )
    except (OSError, CommandFailedError) as exc:
        return (
            f"WARN  {checkout}: could not register the generated-docs merge driver "
            f"({exc}) — merges fall back to textual."
        )
    return f"OK    {checkout}: registered the generated-docs merge driver."
