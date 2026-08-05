"""A checkout must outlive the session that created it (#4194).

Five worktrees holding unpushed work were found registered under a DEAD session's
job dir (``~/.claude/jobs/<session>/tmp/**``) — a directory removed when that job
is deleted. The harness's own ``.claude/worktrees/agent-*`` dirs are the same
class: auto-cleaned as soon as the agent leaves one unchanged, and outside
teatree's ``Worktree`` ledger, so ``workspace emit`` never surfaces them.

One answer to "does this path die with its session?", shared by the three places
that need it: the hand-off barrier enumerates such worktrees to fast-push them,
the checkout-creating seam refuses to create one there, and ``t3 doctor`` names
any registered worktree already on one.

Stdlib plus :mod:`teatree.paths`, so the foundation-layer callers reach it
without pulling in Django.
"""

from pathlib import Path

from teatree.paths import auto_isolated_worktrees_dir

_HARNESS_DIRNAME = ".claude"
_HARNESS_WORKTREES_DIRNAME = "worktrees"
_HARNESS_WORKTREE_PREFIX = "agent-"
_JOBS_DIRNAME = "jobs"


class VolatileCheckoutPathError(RuntimeError):
    """A checkout was asked for under a directory that dies with the session.

    Raised instead of materialising the checkout: work committed there is
    reachable only until the job dir is pruned, and a hand-off naming such a path
    is describing storage that may not outlive its author.
    """


def volatile_reason(path: Path) -> str:
    """Why *path* dies with its session, or ``""`` when it outlives one.

    Two shapes, both under ``.claude``. ``worktrees/agent-*`` is the harness's own
    sub-agent worktree dir. The other is anything strictly BELOW a
    ``jobs/<session>/`` dir — a dispatched agent's scratch, and where the #4194
    incident's five stranded checkouts lived. The job dir itself is not a
    checkout, so only its descendants match.
    """
    if (
        path.name.startswith(_HARNESS_WORKTREE_PREFIX)
        and path.parent.name == _HARNESS_WORKTREES_DIRNAME
        and path.parent.parent.name == _HARNESS_DIRNAME
    ):
        return "a harness sub-agent worktree dir, auto-cleaned when the agent leaves it unchanged"
    parts = path.parts
    # ``jobs`` must be followed by a session id AND at least one more component,
    # so ``.claude/jobs/<session>`` itself is not mistaken for a checkout.
    if any(
        parts[index] == _JOBS_DIRNAME and parts[index - 1] == _HARNESS_DIRNAME for index in range(1, len(parts) - 2)
    ):
        return "a session job dir, removed with the job"
    return ""


def durable_checkout_root() -> Path:
    """Where a checkout that must survive its author is created.

    The auto-isolated worktrees root: swept by ``clean-all``'s raw-orphan pass and
    scanned by ``t3 doctor``, so a checkout left here is ledgered rather than
    silently accumulating.
    """
    return auto_isolated_worktrees_dir()


def resolve_checkout_base_dir(base_dir: str | None) -> str:
    """The parent dir a new checkout may be created in — durable by construction.

    ``None`` resolves to :func:`durable_checkout_root` rather than the system temp
    dir: a dispatched agent's ``TMPDIR`` is routinely its own job scratch, so the
    stdlib default silently produces the very shape this module exists to stop.
    An explicit *base_dir* that is volatile raises rather than being redirected —
    the caller named a directory, and silently writing somewhere else is the kind
    of divergence a hand-off then reports wrongly.
    """
    if base_dir:
        resolved = Path(base_dir).expanduser()
        if reason := volatile_reason(resolved):
            message = (
                f"refusing to create a checkout under {resolved} — {reason}. Durable root: {durable_checkout_root()}"
            )
            raise VolatileCheckoutPathError(message)
        return str(resolved)
    root = durable_checkout_root()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


__all__ = [
    "VolatileCheckoutPathError",
    "durable_checkout_root",
    "resolve_checkout_base_dir",
    "volatile_reason",
]
