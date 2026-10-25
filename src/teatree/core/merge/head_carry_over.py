"""Verdicts, critic rows, attestations and clearances follow the PR patch to a new head (#5450, §17.4).

A merge of ``main`` into a PR branch moves the head, and every record bound to the old head is then
stranded although the PR's patch is the same. :func:`carry_over_head` decides from forge git facts alone —
the old head is an ancestor, every PR-only commit added since is a merge, and the patch id of the PR against
its base is identical at both heads — and either re-records the records at the new head or records WHY not.
Anything it cannot prove refuses: the normal review round then grades the delta. Required CI is untouched; the
merge keystone still reads it live at the new head.

Reviewer and grader identities are never read: carried rows keep the ORIGINAL independent identity, so the
maker≠checker rule is exactly what it was. The conflict-only re-bind (:mod:`teatree.core.merge.conflict_only`)
keeps covering a merge that resolved conflicts, which no patch comparison can vouch for.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from django.db import transaction

from teatree.core.merge.clear_scope import clear_scopes_to_repo
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.models import (
    BranchUpdateAttempt,
    CriticVerdict,
    HeadCarryOver,
    MergeClear,
    ReviewVerdict,
    ReviewVerdictError,
    Ticket,
)
from teatree.core.models.ticket_evidence import recorded_anti_vacuity_attestation

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

SUPPORTED_HOST = "github"


@dataclass(frozen=True, slots=True)
class PatchLineage:
    """What the forge says about the path from *old head* to *new head* of one PR."""

    old_patch_id: str
    new_patch_id: str
    descends: bool
    added_all_merges: bool
    refusal: str = ""

    @property
    def identical(self) -> bool:
        return bool(self.old_patch_id) and self.old_patch_id == self.new_patch_id

    @property
    def refusal_reason(self) -> str:
        """Why the records may not follow, or ``""`` when they may."""
        if self.refusal:
            return self.refusal
        if not self.descends:
            return "not_descendant"
        if not self.added_all_merges:
            return "added_commits_not_merges"
        return "" if self.identical else "patch_differs"


class LineageReader(Protocol):
    def __call__(self, *, slug: str, pr_id: int, host_kind: str, old_head: str, new_head: str) -> PatchLineage | None:
        """The lineage, or ``None`` when the forge could not answer (retried, never read as a verdict)."""
        ...


@dataclass(frozen=True, slots=True)
class _Move:
    slug: str
    pr_id: int
    from_head: str
    to_head: str

    def record(
        self,
        outcome: HeadCarryOver.Outcome,
        *,
        reason: str = "",
        patch_id: str = "",
        carried: dict[str, int] | None = None,
    ) -> HeadCarryOver:
        row, _created = HeadCarryOver.objects.get_or_create(
            slug=self.slug.strip().lower(),
            pr_id=self.pr_id,
            to_head=self.to_head,
            defaults={
                "from_head": self.from_head,
                "outcome": outcome,
                "reason": reason,
                "patch_id": patch_id,
                "carried": carried or {},
            },
        )
        return row

    def refuse(self, reason: str, *, patch_id: str = "") -> HeadCarryOver:
        return self.record(HeadCarryOver.Outcome.REFUSED, reason=reason, patch_id=patch_id)

    def settle(self, lineage: PatchLineage) -> HeadCarryOver:
        """Carry the records when *lineage* proves the patch unchanged, else record why not."""
        if reason := lineage.refusal_reason:
            return self.refuse(reason, patch_id=lineage.new_patch_id)
        try:
            with transaction.atomic():
                if (
                    raced := HeadCarryOver.objects.decided(slug=self.slug, pr_id=self.pr_id, head=self.to_head)
                ) is not None:
                    return raced
                return self.record(
                    HeadCarryOver.Outcome.CARRIED, patch_id=lineage.new_patch_id, carried=_carry_records(self)
                )
        except ReviewVerdictError:
            logger.warning(
                "head carry-over: a verdict of %s#%d could not follow its patch to %s",
                self.slug,
                self.pr_id,
                self.to_head[:8],
            )
            return self.refuse("verdict_not_carryable", patch_id=lineage.new_patch_id)


def carry_over_head(
    *, slug: str, pr_id: int, live_head: str, read_lineage: LineageReader, host_kind: str = SUPPORTED_HOST
) -> HeadCarryOver | None:
    """Carry the records of the PR's newest earlier head to *live_head* when the patch is provably unchanged.

    Idempotent: a head already decided returns its ledger row without asking the forge. ``None`` means nothing
    is recorded — no earlier head holds a record, or the forge could not answer (logged, retried next tick).
    """
    head = live_head.strip().lower()
    if (decided := HeadCarryOver.objects.decided(slug=slug, pr_id=pr_id, head=head)) is not None:
        return decided
    if (prior := _newest_other_head(slug=slug, pr_id=pr_id, head=head)) is None:
        return None
    move = _Move(slug=slug, pr_id=pr_id, from_head=prior, to_head=head)
    if host_kind != SUPPORTED_HOST:
        return move.refuse("unsupported_host")
    lineage = read_lineage(slug=slug, pr_id=pr_id, host_kind=host_kind, old_head=prior, new_head=head)
    if lineage is None:
        logger.warning(
            "head carry-over: could not read the patch lineage of %s#%d from %s to %s; nothing recorded, retrying",
            slug,
            pr_id,
            prior[:8],
            head[:8],
        )
        return None
    return move.settle(lineage)


def _open_clears(slug: str, pr_id: int) -> list[MergeClear]:
    candidates = MergeClear.objects.filter(pr_id=pr_id, consumed_at__isnull=True)
    return [clear for clear in candidates if clear_scopes_to_repo(clear, slug=slug)]


def _newest_other_head(*, slug: str, pr_id: int, head: str) -> str | None:
    """The head, other than *head*, that a record of this PR was most recently written at."""
    stamped: list[tuple[datetime, str]] = [
        *ReviewVerdict.objects.for_pr(slug, pr_id).values_list("recorded_at", "reviewed_sha"),
        *((clear.issued_at, clear.reviewed_sha) for clear in _open_clears(slug, pr_id)),
        *BranchUpdateAttempt.objects.filter(slug__iexact=slug.strip(), pr_id=pr_id).values_list(
            "attempted_at", "head_sha"
        ),
    ]
    if (ticket := resolve_gated_ticket(slug=slug, pr_id=pr_id)) is not None:
        stamped.extend(CriticVerdict.objects.filter(ticket=ticket).values_list("recorded_at", "head_sha"))
    others = [(at, sha.lower()) for at, sha in stamped if sha and sha.lower() != head]
    return max(others)[1] if others else None


def _carry_records(move: _Move) -> dict[str, int]:
    """Re-record every store's rows at the new head; a store already holding a row there is left as it is."""
    ticket = resolve_gated_ticket(slug=move.slug, pr_id=move.pr_id)
    verdicts = _verdicts_to_carry(move)
    critics = _critic_verdicts_to_carry(ticket, move)
    clears = [clear for clear in _open_clears(move.slug, move.pr_id) if clear.reviewed_sha == move.from_head]
    for verdict in verdicts:
        verdict.carry_forward(reviewed_sha=move.to_head)
    for critic in critics:
        critic.carry_forward(head_sha=move.to_head)
    for clear in clears:
        clear.rebind_head(move.to_head)
    return {
        "review_verdicts": len(verdicts),
        "critic_verdicts": len(critics),
        "merge_clears": len(clears),
        "anti_vacuity": int(_carry_attestation(ticket, move)),
    }


def _verdicts_to_carry(move: _Move) -> list[ReviewVerdict]:
    at_pr = ReviewVerdict.objects.for_pr(move.slug, move.pr_id)
    return [] if at_pr.filter(reviewed_sha=move.to_head).exists() else list(at_pr.filter(reviewed_sha=move.from_head))


def _critic_verdicts_to_carry(ticket: Ticket | None, move: _Move) -> list[CriticVerdict]:
    if ticket is None:
        return []
    answered = set(
        CriticVerdict.objects.filter(ticket=ticket, head_sha=move.to_head).values_list("transition", flat=True)
    )
    newest: dict[str, CriticVerdict] = {}
    for verdict in CriticVerdict.objects.filter(ticket=ticket, head_sha=move.from_head).order_by("recorded_at", "pk"):
        newest[verdict.transition] = verdict
    return [verdict for transition, verdict in newest.items() if transition not in answered]


def _carry_attestation(ticket: Ticket | None, move: _Move) -> bool:
    if ticket is None or recorded_anti_vacuity_attestation(ticket, move.to_head).get("head_sha") == move.to_head:
        return False
    return ticket.carry_anti_vacuity_attestation(move.from_head, move.to_head)
