"""The worktree ROOTS the reaper and the doctor scan — one canonical location (#3583).

Worktrees ended up split across two roots: the canonical per-overlay
:func:`teatree.config.worktree_root` that provisioning writes to, and whatever
ad-hoc root an agent happened to `git worktree add` into. A split namespace means
the reaper and `t3 doctor` each see half the picture, so broken checkouts pile up
in the half nobody scans and agents waste time deciding whether a stale sibling
elsewhere is live.

This module is the single answer to "which roots hold teatree worktrees?". The
canonical root is where new worktrees go; the scanned set additionally covers the
roots existing registered worktrees actually live in, so an alternate root is
DRAINED by the reaper rather than left to accumulate — and, once drained, never
written to again, collapsing the split without a manual migration.
"""

from enum import StrEnum
from pathlib import Path

from teatree.config import worktree_root
from teatree.core.models import Worktree
from teatree.core.worktree.checkout_liveness import admin_entry_for, claims_to_be_a_checkout
from teatree.utils.git_run import git_env_without_overrides
from teatree.utils.run import run_allowed_to_fail


class CheckoutState(StrEnum):
    """What a directory was PROVED to be — three answers, and each is an authorisation.

    Read these as verdicts on what a caller may DO, never as claims about what a
    caller can RUN. ``CHECKOUT`` means live: hands off. It covers a checkout git
    resolved here AND one only the clone vouches for, and in the second case git
    commands in that directory still fail — the two collapse into one value because
    the correct action for both is identical, which is nothing.

    ``NOT_A_CHECKOUT`` is the only state that may authorise a destructive release,
    and even then only of a registry ROW: it is never licence to remove a directory,
    which no single context has the evidence to justify. It demands positive proof:
    the directory carries no ``.git`` entry at all and git agrees there is no
    repository. Nothing there ever claimed to be a checkout, so there is no pointer
    this context might merely be failing to follow.

    ``INCONCLUSIVE`` is the load-bearing one — the UNKNOWN a fail-open reaper
    keeps. git declining to speak (dubious ownership, a permission error, a
    missing binary) and a checkout whose recorded admin dir is absent from THIS
    execution context both land here: each looks exactly like a dead checkout to a
    boolean probe, and a boolean probe is what let the reaper treat "git would not
    answer" as "this dir holds nothing".
    """

    CHECKOUT = "checkout"
    NOT_A_CHECKOUT = "not-a-checkout"
    INCONCLUSIVE = "inconclusive"


# git's own wording for "there is no repository here". Every other fatal leaves
# the question open.
_NOT_A_CHECKOUT_STDERR = ("not a git repository", "not a working tree", "invalid gitfile format")


def probe_checkout(path: Path, *, clone: Path | None = None) -> CheckoutState:
    """Classify *path* as a live checkout, a proven non-checkout, or unanswerable.

    The one probe the broken-dir reaper, the row reaper and ``t3 doctor`` share,
    so they can never disagree about which dirs are broken — and the same probe
    whose failure the setup-time ``is not a git checkout`` WARN reports. Runs with
    every ``GIT_*`` override stripped so an ambient hook environment cannot answer
    for a different repo.

    Pass *clone* — the source clone as THIS context reaches it — whenever the
    caller knows it. Without it the probe can only ever downgrade an unresolvable
    pointer to ``INCONCLUSIVE``; with it, the clone's own admin entry proves the
    checkout live and the answer is exact.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            expected_codes=None,
            env=git_env_without_overrides(),
        )
    except OSError:
        return CheckoutState.INCONCLUSIVE
    if result.returncode == 0 and result.stdout.strip():
        return CheckoutState.CHECKOUT
    return _refusal_state(path, clone=clone, stderr=result.stderr.lower())


def _refusal_state(path: Path, *, clone: Path | None, stderr: str) -> CheckoutState:
    """What git's refusal proves — which is far less than its wording suggests.

    ``fatal: not a git repository`` is git's answer both for a directory that
    never held one and for a checkout whose recorded admin dir this context
    cannot reach, so the wording alone cannot separate them. The directory's own
    ``.git`` entry can: a checkout that staked the claim is never called dead on a
    pointer this context failed to follow.
    """
    if not claims_to_be_a_checkout(path):
        return CheckoutState.NOT_A_CHECKOUT if _reads_as_no_repository(stderr) else CheckoutState.INCONCLUSIVE
    if clone is not None and admin_entry_for(path, clone) is not None:
        return CheckoutState.CHECKOUT
    return CheckoutState.INCONCLUSIVE


def _reads_as_no_repository(stderr: str) -> bool:
    return any(marker in stderr for marker in _NOT_A_CHECKOUT_STDERR)


def canonical_worktree_root() -> Path:
    """Where NEW worktrees are created — the one location everything converges on."""
    return worktree_root()


def registered_worktree_roots() -> set[Path]:
    """The parent dirs of every registered worktree's on-disk checkout.

    A row whose checkout sits outside :func:`canonical_worktree_root` contributes
    its own parent, which is how an alternate root becomes visible to the scans.
    """
    return {Path(wt.worktree_path).parent for wt in Worktree.objects.all() if wt.worktree_path}


def scanned_worktree_roots(workspace: Path) -> tuple[Path, ...]:
    """Every root a cleanup/health pass must scan, canonical root first.

    *workspace* is the caller's resolved workspace dir, included so a pass driven
    from an explicit workspace never misses it when config resolution disagrees.
    """
    roots = [canonical_worktree_root(), workspace, *sorted(registered_worktree_roots())]
    return tuple(dict.fromkeys(root.expanduser() for root in roots))


def worktrees_outside_the_canonical_root() -> list[Worktree]:
    """Registered worktrees whose checkout is not under :func:`canonical_worktree_root`.

    The namespace-split signal `t3 doctor` reports: each of these is invisible to
    a pass that scans only the canonical root.
    """
    canonical = canonical_worktree_root().expanduser()
    outside: list[Worktree] = []
    for worktree in Worktree.objects.all():
        path = worktree.worktree_path
        if path and not Path(path).expanduser().is_relative_to(canonical):
            outside.append(worktree)
    return outside


__all__ = [
    "CheckoutState",
    "canonical_worktree_root",
    "probe_checkout",
    "registered_worktree_roots",
    "scanned_worktree_roots",
    "worktrees_outside_the_canonical_root",
]
