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

from pathlib import Path

from teatree.paths import teatree_source_root
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

_DRIVER_NAME = "regenerate generated docs on conflict (souliane/teatree#3582)"
_DRIVER_SCRIPT = Path("scripts/hooks/git_merge_generated.py")


def driver_command() -> str:
    """The ``merge.generated.driver`` value, with the driver script named ABSOLUTELY.

    ``uv run python`` supplies the venv interpreter with teatree + Django installed.
    git runs a merge driver with cwd at the top of the working tree, which in a fork
    that vendors core is the FORK root — where ``scripts/hooks/`` is the fork's own
    and holds no driver script. A repo-relative command therefore died with
    ``can't open file '<fork>/scripts/hooks/git_merge_generated.py'`` on every merge.
    :func:`~teatree.paths.teatree_source_root` is layout-independent (pure stdlib, so
    this module stays Django-free), giving the repo root in a plain clone and
    ``<repo>/vendor/teatree`` in a fork.
    """
    return f"uv run python {teatree_source_root() / _DRIVER_SCRIPT} %O %A %B %P"


def install_merge_driver(checkout: Path) -> str:
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
            ["git", "-C", str(checkout), "config", "merge.generated.driver", driver_command()],
        )
    except (OSError, CommandFailedError) as exc:
        return (
            f"WARN  {checkout}: could not register the generated-docs merge driver "
            f"({exc}) — merges fall back to textual."
        )
    return f"OK    {checkout}: registered the generated-docs merge driver."
