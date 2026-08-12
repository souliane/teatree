"""Register the ``generated`` git merge driver in every checkout (#3582).

``.gitattributes`` marks the generated docs ``merge=generated``, but that only
names a driver — the driver's *command* lives in per-clone ``.git/config`` and is
never committed. This installs the ``git config merge.generated.driver`` value so
a checkout's merges of those paths carry the driver's regeneration advisory. The
merge RESULT is the same either way (the driver merges textually, exactly as git
would — souliane/teatree#4259); an unregistered checkout simply never hears that
the doc it just merged needs regenerating.

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


def driver_command(checkout: Path | None = None) -> str:
    """The ``merge.generated.driver`` value, naming the driver script for *checkout*.

    ``uv run python`` supplies the venv interpreter with teatree + Django installed.
    :func:`~teatree.paths.teatree_source_root` is layout-independent (pure stdlib, so
    this module stays Django-free), giving the repo root in a plain clone and
    ``<repo>/vendor/teatree`` in a fork.

    The script is named RELATIVE to *checkout* whenever it lives inside it. git runs a
    merge driver with cwd at the top of the working tree, so ``scripts/hooks/…`` in a
    plain clone and ``vendor/teatree/scripts/hooks/…`` in a fork both resolve — a
    core-relative command (the original shape) named nothing in a fork, which is the
    ``can't open file '<fork>/scripts/hooks/git_merge_generated.py'`` failure, but a
    CHECKOUT-relative one is exactly the path git will look for.

    Relative matters beyond tidiness: ``.git/config`` is per-clone, and a clone
    bind-mounted into a container is the SAME config file under two different absolute
    paths. With an absolute command, whichever side ran ``t3 setup`` last silently
    pointed the other side's merges at a path that does not exist there, and a driver
    git cannot run emits no regeneration advisory at all. A relative command is
    identical on both sides, so neither can clobber the other.

    A script outside *checkout* (an installed teatree serving a repo that does not
    vendor it, and the no-checkout call) has no relative form and stays absolute.
    """
    script = teatree_source_root() / _DRIVER_SCRIPT
    if checkout is not None:
        try:
            script = script.resolve().relative_to(checkout.resolve())
        except (OSError, ValueError):
            script = teatree_source_root() / _DRIVER_SCRIPT
    return f"uv run python {script} %O %A %B %P"


def install_merge_driver(checkout: Path) -> str:
    """Register the ``generated`` merge driver in *checkout*; return a status line.

    Never raises — a git failure degrades to a ``WARN`` line so setup and
    provisioning continue (the driver only adds an advisory, so an unregistered
    checkout merges the same bytes; the CI sync checks remain the backstop).
    """
    try:
        run_allowed_to_fail(
            ["git", "-C", str(checkout), "config", "merge.generated.name", _DRIVER_NAME],
        )
        run_allowed_to_fail(
            ["git", "-C", str(checkout), "config", "merge.generated.driver", driver_command(checkout)],
        )
    except (OSError, CommandFailedError) as exc:
        return (
            f"WARN  {checkout}: could not register the generated-docs merge driver "
            f"({exc}) — merges here get no regeneration advisory."
        )
    return f"OK    {checkout}: registered the generated-docs merge driver."
