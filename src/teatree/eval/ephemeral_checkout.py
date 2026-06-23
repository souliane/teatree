"""A throwaway, isolated teatree checkout for sub-agent-spawning eval scenarios.

The metered ``sdk`` lane runs some scenarios (``orchestrator_delegates_*``,
``full_speed_*``, ``team_mate_*``, ``delegates_under_load_*``) that SPAWN a
sub-agent and instruct it to do real, destructive git work — create files, switch
branches, commit. The :class:`~teatree.eval.isolation.isolated_claude_env`
clean-room only redirects the developer's *personal context* roots (``HOME`` etc.)
and a neutral cwd; it does NOT stop the spawned sub-agent from locating the REAL
teatree clone. The sub-agent finds it two ways a neutral cwd cannot block:

*   the **editable install** — ``python -c "import teatree; print(teatree.__file__)"``
    resolves through the install's ``.pth`` straight into the developer's
    ``src/teatree`` (the real clone), so the sub-agent learns the real repo path
    even from a ``/tmp`` cwd;
*   the **shared ``.git``** — a worktree of the real clone shares its object store
    and refs, so a commit/branch-switch reaches the real repository.

Running from a ``/tmp`` cwd therefore did NOT protect the developer's clone (the
issue this module fixes was observed corrupting the main clone and two live
worktrees). The fix is a per-run EPHEMERAL CHECKOUT: a ``git worktree --detach``
in a temp dir whose own working tree + detached ``HEAD`` absorb every write the
sub-agent makes, plus an :func:`ephemeral_checkout_env` overlay that redirects the
TWO real-clone resolution levers at it — ``PYTHONPATH`` (so ``import teatree``
resolves into the ephemeral ``src``, NOT the editable ``.pth``) and the git
discovery vars (so ``git`` operations resolve to the ephemeral working tree).
The checkout is torn down when the run finishes, so nothing leaks.

When the real teatree root cannot be located (a packaged install with no source
tree, a corrupt repo) :func:`provision_ephemeral_checkout` raises
:class:`EphemeralCheckoutError` — the spawning scenario REFUSES to run on the real
clone rather than fall back to it. That is the safe failure: a refused scenario is
a clean skip, a sub-agent on the real clone is repo corruption.
"""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.utils.git_run import run as git_run
from teatree.utils.git_worktree import worktree_add_at_ref, worktree_remove


class EphemeralCheckoutError(RuntimeError):
    """Raised when an isolated ephemeral checkout cannot be provisioned.

    A spawning scenario REFUSES to run rather than let the SDK sub-agent fall back
    to the developer's real clone — running on the real clone is the exact repo
    corruption this isolation exists to prevent.
    """


def resolve_teatree_repo_root() -> Path | None:
    """The git working tree root of the installed teatree source, or ``None``.

    Resolves through the editable install's own location
    (``teatree/__init__.py`` -> ``src/teatree`` -> ``src`` -> repo root) and
    confirms it is a real git working tree via ``git rev-parse --show-toplevel``.
    Returns ``None`` when teatree is not running from a git checkout (a packaged
    install) — the caller then REFUSES the spawning scenario rather than guess.
    """
    source_root = Path(__file__).resolve().parents[3]
    toplevel = git_run(repo=str(source_root), args=["rev-parse", "--show-toplevel"])
    return Path(toplevel) if toplevel else None


@contextmanager
def provision_ephemeral_checkout() -> Iterator[Path]:
    """Yield a throwaway ``git worktree --detach`` of the real teatree clone.

    The yielded directory is a detached worktree: its own working tree and
    detached ``HEAD`` absorb every file write, branch switch, and commit the SDK
    sub-agent makes, so none reach the developer's real clone or live worktrees.
    The worktree is removed (``git worktree remove --force``) and its temp parent
    deleted on context exit, success or failure.

    Raises :class:`EphemeralCheckoutError` when the real teatree root cannot be
    located or the worktree cannot be created — the spawning scenario then refuses
    to run on the real clone.
    """
    repo_root = resolve_teatree_repo_root()
    if repo_root is None:
        msg = (
            "cannot provision an isolated ephemeral checkout: teatree is not running "
            "from a resolvable git clone (packaged install or corrupt repo). The "
            "sub-agent-spawning scenario REFUSES to run rather than do destructive "
            "git work on the real clone."
        )
        raise EphemeralCheckoutError(msg)

    parent = Path(tempfile.mkdtemp(prefix="t3-eval-ephemeral-checkout-"))
    checkout = parent / "teatree"
    try:
        if not worktree_add_at_ref(str(repo_root), str(checkout), "HEAD"):
            msg = (
                f"cannot provision an isolated ephemeral checkout at {checkout}: "
                "git worktree add failed. The sub-agent-spawning scenario REFUSES "
                "to run on the real clone."
            )
            raise EphemeralCheckoutError(msg)
        yield checkout
    finally:
        worktree_remove(str(repo_root), str(checkout))
        shutil.rmtree(parent, ignore_errors=True)


def ephemeral_checkout_env(base_env: dict[str, str], checkout: Path) -> dict[str, str]:
    """Overlay *base_env* so teatree + git resolve to the ephemeral *checkout*.

    Returns a NEW env dict (never mutates *base_env*) that redirects the two levers
    by which an SDK sub-agent would otherwise reach the developer's real clone:

    *   ``PYTHONPATH`` is prepended with ``<checkout>/src`` so ``import teatree``
        resolves into the ephemeral checkout, NOT the editable install's ``.pth``
        that points at the real ``src/teatree``;
    *   ``GIT_DIR`` / ``GIT_WORK_TREE`` are CLEARED and ``GIT_CEILING_DIRECTORIES``
        is unset so ``git`` rediscovers the repo from the (ephemeral) cwd rather
        than inheriting a pin to the real clone.

    cwd is the caller's responsibility (the SDK ``cwd`` option) — this overlays the
    ``env`` half so the resolution lands in the throwaway even when the sub-agent
    walks up from a neutral cwd.
    """
    env = dict(base_env)
    ephemeral_src = str(checkout / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{ephemeral_src}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else ephemeral_src
    for git_pin in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
        env.pop(git_pin, None)
    return env


__all__ = [
    "EphemeralCheckoutError",
    "ephemeral_checkout_env",
    "provision_ephemeral_checkout",
    "resolve_teatree_repo_root",
]
