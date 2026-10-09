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

#: Where a checkout carries the driver script, in probe order: a plain teatree clone,
#: then a fork that vendors core under ``vendor/teatree``.
_CHECKOUT_RELATIVE_SCRIPTS = (_DRIVER_SCRIPT, Path("vendor") / "teatree" / _DRIVER_SCRIPT)


def driver_command(checkout: Path | None = None) -> str:
    """The ``merge.generated.driver`` value, naming the driver script for *checkout*.

    ``uv run python`` supplies the venv interpreter with teatree + Django installed.

    The path is CHECKOUT-relative, and it is derived by asking *checkout* what it
    carries rather than by locating the installed teatree. git runs a merge driver with
    cwd at the top of the working tree, so a relative command is the path git will
    actually look for, and — crucially — it is the SAME string in every venue.

    Deriving it from the installed teatree instead was the defect: ``.git/config`` is
    per-CLONE and shared by every worktree cut from it, and teatree's own workflow
    registers from a worktree, whose vendored core is not under the clone being
    registered. The relative form was therefore unreachable in exactly the normal case,
    and the absolute fallback wrote one venue's path into a config the main clone, every
    sibling worktree and the bind-mounted container all read — so their merges ran
    ``can't open file '<other-venue>/…/git_merge_generated.py'`` and silently lost the
    regeneration advisory.

    A *checkout* carrying no driver script (an installed teatree serving a repo that
    does not vendor it, and the no-checkout call) has no relative form and stays
    absolute — there is nothing in that tree for a relative path to name.
    """
    for relative in _CHECKOUT_RELATIVE_SCRIPTS:
        if checkout is not None and (checkout / relative).is_file():
            return f"uv run python {relative} %O %A %B %P"
    return f"uv run python {teatree_source_root() / _DRIVER_SCRIPT} %O %A %B %P"


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
