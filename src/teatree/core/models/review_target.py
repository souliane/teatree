"""Which pull request and head is a reviewing task answerable for? — one resolver (#4308).

Two consumers needed this answer and each grew its own half. The READER
(:func:`~teatree.core.models.phase_landing.phase_landing_evidence`) carried both sources; the
WRITER (:mod:`teatree.agents.attempt_recorder`) read only the linked
:class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`, so a reviewing task
without one silently DISCARDED a well-formed ``review_verdict`` envelope and completed exit
0 — a real HOLD surviving only inside a ``TaskAttempt.result`` no merge guard reads, on a PR
that otherwise presented as CLEAN. A split answer is what let the writer's key and the
reader's key disagree; one resolver is what stops them disagreeing again.

:func:`verdict_at` is the read-back the writer verifies its own persistence with, and it
queries exactly what the consumers do — :meth:`ReviewVerdict.objects.for_pr`'s
``slug__iexact``, the predicate the merge gate's ``authorizing_verdict_at`` and the
landed-work guard both read. A stricter read-back would refuse a row those consumers can
see, which is a false "never persisted" on a forge slug spelled another way.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models.auto_review_dispatch import LOOP_SCANNER_HOLDER, AutoReviewDispatch
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.task import Task


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    """The pull request a reviewing task's verdict binds to, and the head it judged.

    ``head_sha`` is empty when the PR is known but the tree under review is not — a
    reviewer ticket whose ``reviewed_sha`` was never stamped. That is a distinct state from
    "no PR at all" (which resolves to ``None``): the verdict IS owed to a merge guard here,
    so the writer refuses rather than dropping it, while a task answerable for no PR keeps
    its pre-existing completion.
    """

    slug: str
    pr_id: int
    head_sha: str
    #: The identity this dispatch path holds the per-MR review lock under, or ``""`` for a
    #: path that took none and cannot know whose identity did (``MRReviewLock.resolve``
    #: releases on the unnamed one, never on a named non-holder).
    lock_holder: str = ""
    #: The per-head claim table that ARMED this run — the #68 dispatch ledger when the task
    #: carries its FK, else the codex / self-PR marker. Read by the refusal terminal, which
    #: is RUN-scoped and may touch only the claim that armed the run; the RESOLVE terminal
    #: ignores it, because a recorded verdict is a fact about the TREE and retires every
    #: claim on it (#4530). Defaults to the marker so a path with no claim at all resolves
    #: to a no-op lookup rather than to the ledger that holds the review lock.
    armed_by: "type[AutoReviewDispatch | CodexReviewMarker]" = CodexReviewMarker


def review_target_for_task(task: "Task") -> ReviewTarget | None:
    """The PR + head *task*'s review is answerable for, or ``None`` when it is answerable for none.

    Most-authoritative first: the #68 auto-review dispatch carries ``(slug, pr_id,
    head_sha)`` and holds the per-MR lock. Failing that, a reviewer-role ticket IS the PR —
    its ``issue_url`` names it and ``extra["reviewed_sha"]`` names the head a later push
    rewrites, so the answer is always about a head some reviewer actually judged.
    """
    dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if dispatch is not None:
        return ReviewTarget(
            slug=dispatch.slug,
            pr_id=dispatch.pr_id,
            head_sha=dispatch.head_sha.strip(),
            lock_holder=LOOP_SCANNER_HOLDER,
            armed_by=AutoReviewDispatch,
        )
    reviewed_pr = pr_ref_from_url(task.ticket.issue_url)
    if reviewed_pr is None:
        return None
    return ReviewTarget(
        slug=reviewed_pr.slug,
        pr_id=reviewed_pr.pr_id,
        head_sha=str((task.ticket.extra or {}).get("reviewed_sha", "")).strip(),
    )


def verdict_at(target: ReviewTarget) -> ReviewVerdict | None:
    """The verdict durably recorded for *target*, or ``None`` — the shared read-back.

    An empty ``head_sha`` can match nothing: a verdict binds to the exact tree it judged, so
    "some verdict at an unknown head" is not an answer any consumer accepts. The slug is
    matched case-insensitively through :meth:`~ReviewVerdict.objects.for_pr`, because a
    forge slug spelled ``Owner/Repo`` names the PR the guards resolve as ``owner/repo``.
    """
    if not target.head_sha:
        return None
    return ReviewVerdict.objects.for_pr(target.slug, target.pr_id).filter(reviewed_sha=target.head_sha.lower()).first()


__all__ = ["ReviewTarget", "review_target_for_task", "verdict_at"]
