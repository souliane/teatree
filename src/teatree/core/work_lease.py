"""The lease keys on the BRANCH and PR being worked, not only the ticket (#3561).

The ticket lease excluded two lifecycle actors from one ticket, but had no
visibility into work done at the raw git/PR level — a hand-opened PR, an ad-hoc
`git worktree add`, a direct branch push. Work that never entered the lifecycle
was unlocked by construction, so an interactive session pushing fixes to a PR
branch and the loop claiming that PR's ticket were invisible to each other; had
the loop been in an implement phase, both would have pushed divergent commits to
the same branch.

A claim is registered at the two seams where branch/PR work becomes real — PR
reconciliation and worktree adoption — under every identity the work is known by:
the branch, the PR, and the ISSUE it serves. The issue identity is the join that
makes the raw work visible to the lifecycle: :func:`foreign_work_holder` reads
it, and the issue-implementer claim defers to a live foreign holder.

The rows are :class:`~teatree.core.models.LoopLease` records under the ``work:``
prefix, so this reuses the backend-agnostic CAS (a conditional ``UPDATE``, never
``select_for_update`` — the #786 B1 SQLite lesson) with no new table. The TTL is
the fallback release: an interactive session that walks away never wedges the
loop past it, and the loop DEFERS rather than blocks — the least invasive of the
issue's options, and the one that never stands in a human's way.
"""

import hashlib
from dataclasses import dataclass

from teatree.core.models import LoopLease

_WORK_PREFIX = "work:"

#: A branch/PR claim not re-registered within this window lapses. Sized for an
#: interactive working session: long enough that a human mid-PR is not preempted
#: between pushes, short enough that an abandoned claim frees the loop within one
#: working period rather than forever.
DEFAULT_WORK_TTL_SECONDS = 14400


def _slot(kind: str, identity: str) -> str:
    """``work:<kind>:<digest>`` — a bounded, collision-free key for any identity.

    The identity (a URL, an ``org/repo@branch``) is hashed rather than embedded,
    so an arbitrarily long or punctuated value still yields a valid fixed-width
    lease name; ``kind`` stays readable for an operator reading the table.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{_WORK_PREFIX}{kind}:{digest}"


def branch_slot(repo: str, branch: str) -> str:
    """Lease key for work on ``branch`` of ``repo``."""
    return _slot("branch", f"{repo}@{branch}")


def pr_slot(pr_url: str) -> str:
    """Lease key for work on the pull request at ``pr_url``."""
    return _slot("pr", pr_url)


def issue_slot(issue_url: str) -> str:
    """Lease key for work serving ``issue_url`` — the join the lifecycle claim reads."""
    return _slot("issue", issue_url)


@dataclass(frozen=True, slots=True)
class WorkIdentity:
    """Every identity one piece of branch/PR work is known by.

    A caller supplies whichever it knows: the PR reconciler has a PR URL and an
    issue, the worktree-adopt seam a repo + branch and an issue. Each known
    identity becomes its own lease slot, so a later reader that knows only ONE of
    them still finds the claim.
    """

    repo: str = ""
    branch: str = ""
    pr_url: str = ""
    issue_url: str = ""

    def slots(self) -> list[str]:
        slots: list[str] = []
        if self.repo and self.branch:
            slots.append(branch_slot(self.repo, self.branch))
        if self.pr_url:
            slots.append(pr_slot(self.pr_url))
        if self.issue_url:
            slots.append(issue_slot(self.issue_url))
        return slots


def register_work_claim(
    identity: WorkIdentity, *, owner: str, ttl_seconds: int = DEFAULT_WORK_TTL_SECONDS
) -> list[str]:
    """Claim every identity of this work; return the slots now held by *owner*.

    Idempotent — re-registering the same owner is a renewal, so both seams may
    fire for one piece of work. A slot a DIFFERENT live owner holds is not stolen
    and is simply absent from the result, so the caller learns what it owns.
    """
    return [
        slot for slot in identity.slots() if LoopLease.objects.acquire(slot, owner=owner, lease_seconds=ttl_seconds)
    ]


def foreign_work_holder(identity: WorkIdentity, *, owner: str) -> str:
    """The owner of a LIVE claim on this work that is not *owner*, else ``""``.

    The read a lifecycle claim gates on. An expired claim reports nothing — the
    TTL is the release, so an abandoned session can never wedge the loop.
    """
    for slot in identity.slots():
        holder = _live_owner(slot)
        if holder and holder != owner:
            return holder
    return ""


def _live_owner(slot: str) -> str:
    from django.utils import timezone  # noqa: PLC0415 — deferred: keeps this module importable pre-Django

    row = LoopLease.objects.filter(name=slot).values("owner", "lease_expires_at").first()
    if not row or not (row["owner"] or ""):
        return ""
    expires = row["lease_expires_at"]
    return str(row["owner"]) if expires is not None and expires > timezone.now() else ""


def release_work_claim(identity: WorkIdentity, *, owner: str) -> None:
    """Release every slot *owner* holds for this work (CAS on owner — a non-owner is a no-op)."""
    for slot in identity.slots():
        LoopLease.objects.release(slot, owner=owner)


__all__ = [
    "DEFAULT_WORK_TTL_SECONDS",
    "WorkIdentity",
    "branch_slot",
    "foreign_work_holder",
    "issue_slot",
    "pr_slot",
    "register_work_claim",
    "release_work_claim",
]
