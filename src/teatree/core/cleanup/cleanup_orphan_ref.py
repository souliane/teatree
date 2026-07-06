"""Branch-ref-gone reap/keep decision for worktree teardown (the disk lever).

Split out of :mod:`teatree.core.cleanup.cleanup` to keep that module under the
module-health LOC cap. Owns the post-merge-delete signal: when a forge deletes
a worktree's branch after merge, ``refs/heads/<branch>`` vanishes and the
data-loss probe ``git log <ref> --not --remotes`` exits 128 ("unknown
revision") because the worktree HEAD is a dangling symref. cleanup used to read
that probe failure as "cannot verify pushed → keep", so merged debris
accumulated until the disk filled.

Branch-ref-gone is itself the post-merge-delete signal: the decision is made by
the worktree's *last* HEAD SHA (recovered from its per-worktree reflog, which
survives the ref deletion) and its containment in a remote — reaping only work
that is POSITIVELY confirmed on a remote, keeping everything else.

Imports ``_EffectiveTarget`` only under :data:`TYPE_CHECKING` so there is no
runtime import cycle with :mod:`teatree.core.cleanup.cleanup` (which calls these).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.cleanup.cleanup import _EffectiveTarget

# A full git sha is 40 hex chars; the refusal lists at most this many.
_SUBJECT_PREVIEW_LIMIT = 3


@dataclass(frozen=True)
class OrphanRefDecision:
    """The reap/keep verdict for a worktree whose checked-out branch ref is gone.

    ``recovered_sha`` is the worktree's last HEAD SHA (from its per-worktree
    reflog), or ``None`` when it could not be recovered. ``in_remote`` is the
    POSITIVE-confirmation reap signal: ``True`` only when ``recovered_sha`` is
    resolvable AND contained in some remote. ``unsynced`` holds the
    ``"<sha> <subject>"`` lines when the recovered SHA is on no remote, so the
    caller can raise the accurate data-loss refusal instead of the cryptic
    probe-failure one.
    """

    recovered_sha: str | None
    in_remote: bool
    unsynced: list[str]


def classify_orphan_ref(target: "_EffectiveTarget") -> OrphanRefDecision:
    """Decide reap/keep for a branch-ref-gone worktree from its recovered HEAD.

    Fails closed: an unrecoverable HEAD or an erroring containment probe yields
    ``in_remote=False`` with no ``unsynced`` lines, so the caller keeps the
    refusal — only a positively-in-a-remote SHA authorizes the reap.

    Only meaningful when the worktree dir is present (``target.ref`` is the
    literal ``HEAD``); a gone dir has no reflog to recover.
    """
    if target.ref != git.DETACHED_HEAD:
        return OrphanRefDecision(recovered_sha=None, in_remote=False, unsynced=[])
    sha = git.recovered_head_sha_after_ref_gone(target.probe_repo)
    if not sha:
        return OrphanRefDecision(recovered_sha=None, in_remote=False, unsynced=[])
    try:
        unsynced = git.commits_absent_from_all_remotes(target.probe_repo, sha)
    except CommandFailedError:
        return OrphanRefDecision(recovered_sha=sha, in_remote=False, unsynced=[])
    return OrphanRefDecision(recovered_sha=sha, in_remote=not unsynced, unsynced=unsynced)


def raise_or_reap_orphan_ref(worktree: Worktree, target: "_EffectiveTarget", exc: CommandFailedError) -> None:
    """Resolve the rc=128 probe failure: reap a remote-confirmed orphan, else refuse.

    The branch ref is gone (post-merge deletion → dangling HEAD made the probe
    exit 128). :func:`classify_orphan_ref` recovers the last HEAD SHA and its
    remote containment, giving three outcomes:

    HEAD-in-remote returns (reap) — POSITIVE proof the work shipped. HEAD
    recovered but on no remote raises the accurate "on NO remote (data loss)"
    refusal naming the SHA (keep), not the cryptic probe-failure message. An
    unrecoverable HEAD or an erroring containment probe keeps the original
    fail-closed "could not verify the branch is pushed" refusal (keep on
    uncertainty).
    """
    decision = classify_orphan_ref(target)
    if decision.in_remote:
        return
    if decision.recovered_sha and decision.unsynced:
        preview = decision.unsynced[:_SUBJECT_PREVIEW_LIMIT]
        shas = ", ".join(preview)
        if len(decision.unsynced) > _SUBJECT_PREVIEW_LIMIT:
            shas += ", …"
        msg = (
            f"{worktree.repo_path} ({target.label}): "
            f"refused teardown — {len(decision.unsynced)} commit(s) on NO remote (data loss): "
            f"{shas}. Push the branch or pass force=True to discard."
        )
        raise RuntimeError(msg) from exc
    msg = (
        f"{worktree.repo_path} ({target.label}): "
        f"refused teardown — could not verify the branch is pushed "
        f"(git probe failed: {exc}). Push the branch or pass force=True to discard."
    )
    raise RuntimeError(msg) from exc
