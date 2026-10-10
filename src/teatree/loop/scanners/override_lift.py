"""Propose lifting a manual override whose reason has cleared — never lift one (A8).

Nothing here writes an override. An override is a person's decision, so the factory's
whole job is to notice when the condition that justified it is gone and ASK. That is only
possible because an override carries the REASON it was set: without one the only policy
available is a timer, which is exactly the auto-revert A6 was retracted for.

Two classes of reason, two behaviours. A MACHINE-CHECKABLE reason names a condition this
box can re-read from its own tables — ``pr:<url>`` settled, ``issue:<url>`` done — so when
the condition clears, one deduped question goes out. A PROSE reason cannot be judged, so
it earns an AGED reminder instead: how long the override has stood, and that it is still
standing. Ageing is a reason to ask, never to clear.

:data:`_CHECKERS` is the extension point. A prefix belongs there only when the condition
is answerable WITHOUT a forge round-trip, because this runs on the hourly housekeeping
pass and a network read per override would make the reminder cost more than it saves.

``auto:``-prefixed reasons are skipped whole: the system set those and the system clears
them on its own signal (the usage-window re-arm), so proposing a lift would be the
factory asking the owner to confirm its own bookkeeping.

The scanner and the proposal logic share one module deliberately: the scanner is a
four-line wrapper, and a separate one would have to sit in the orchestration layer the
scanner package may not import.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from django.utils import timezone

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

#: A reason the system set for itself — its own clearing path already exists.
SYSTEM_REASON_PREFIX = "auto:"

#: How long a prose-reasoned override may stand before it earns a reminder.
PROSE_REMINDER_AGE = dt.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class OverrideProposal:
    """One override the owner should be asked about, and why it is being raised now."""

    loop_name: str
    runs: bool
    reason: str
    summary: str
    question: str
    set_at: dt.datetime | None
    card: QuestionCard

    @property
    def dedupe_marker(self) -> str:
        stamp = f"{self.set_at.timestamp():.0f}" if self.set_at else ""
        return f"override-lift:{self.loop_name}@{stamp}"


def override_lift_proposals(now: dt.datetime | None = None) -> list[OverrideProposal]:
    """Every standing manual override the owner should be asked about, in loop order.

    Pure over the DB read — it decides nothing and writes nothing. The caller records the
    questions; this answers only "which overrides look liftable, and how do I say so".
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    moment = now or timezone.now()
    proposals = []
    for row in Loop.objects.exclude(enabled=None).order_by("name"):
        proposal = _proposal_for(
            row.name, runs=bool(row.enabled), reason=row.override_reason, set_at=row.override_set_at, now=moment
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def raise_override_lift_questions(now: dt.datetime | None = None) -> int:
    """Ask about each liftable override exactly once; return how many questions are open.

    Deduped per override (its loop and when it was set), so an override that stands for months
    costs one question rather than one per pass, and only a newly set override is asked again.
    """
    from teatree.core.models.deferred_question import (  # noqa: PLC0415 — deferred: ORM needs the app registry
        DeferredQuestion,
    )

    proposals = override_lift_proposals(now)
    for proposal in proposals:
        try:
            DeferredQuestion.record(
                proposal.question,
                dedupe_marker=proposal.dedupe_marker,
                card=proposal.card,
            )
        except Exception:
            logger.exception("override-lift question failed for %r — the override is untouched", proposal.loop_name)
    return len(proposals)


_LIFT = CardOption("Lift it", "The presets decide again whether the job runs.")
_KEEP = CardOption("Keep it", "The override stays exactly as it is.")


def _proposal_for(
    loop_name: str, *, runs: bool, reason: str, set_at: dt.datetime | None, now: dt.datetime
) -> OverrideProposal | None:
    if reason.startswith(SYSTEM_REASON_PREFIX):
        return None
    forced = "on" if runs else "off"
    machine_checkable = _reason_is_machine_checkable(reason)
    if machine_checkable and not _condition_cleared(reason):
        return None
    if not machine_checkable and (set_at is None or now - set_at < PROSE_REMINDER_AGE):
        return None
    days = (now - set_at).days if set_at else 0
    if machine_checkable:
        summary = (
            f"The manual override forcing {loop_name!r} {forced} says {reason!r}, and that condition "
            "now reads as resolved. Lift the override so the preset decides again?"
        )
        question = f"Can I lift the manual override that forces a background job {forced}?"
        why = "The reason you gave for the override now reads as resolved."
        checked = ("The reason given for the override reads as resolved on this box.",)
        options = (replace(_LIFT, recommended=True), _KEEP)
    else:
        summary = (
            f"The manual override forcing {loop_name!r} {forced} has stood for {days} days ({reason!r}). "
            "Nothing here can judge whether it still applies — lift it, or leave it standing?"
        )
        question = f"Should the manual override that forces a background job {forced} stay in place?"
        why = f"The override has stood for {days} days, and nothing here can judge whether it still applies."
        checked = ("The reason given for the override cannot be checked here.", f"It has stood for {days} days.")
        options = (replace(_KEEP, recommended=True), _LIFT)
    card = QuestionCard(
        decision=OwnerDecision.PRODUCT_SCOPE,
        checked=checked,
        blocker="Only the owner can say whether the override still applies.",
        why=why,
        options=options,
    ).with_quote(question, reason)
    return OverrideProposal(
        loop_name=loop_name, runs=runs, reason=reason, summary=summary, question=question, set_at=set_at, card=card
    )


def _reason_is_machine_checkable(reason: str) -> bool:
    return bool(_checker_for(reason))


def _condition_cleared(reason: str) -> bool:
    checker = _checker_for(reason)
    if checker is None:
        return False
    try:
        return checker(reason)
    except Exception:
        logger.warning("override-reason check failed for %r — treating it as still standing", reason, exc_info=True)
        return False


def _pr_is_settled(reason: str) -> bool:
    from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: ORM needs the app registry

    settled = (PullRequest.State.MERGED, PullRequest.State.CLOSED)
    return PullRequest.objects.filter(url=_url_of(reason), state__in=settled).exists()


def _issue_is_done(reason: str) -> bool:
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM needs the app registry

    return Ticket.objects.filter(issue_url=_url_of(reason), state__in=Ticket.marker_release_states()).exists()


def _url_of(reason: str) -> str:
    return reason.split(":", 1)[1].strip()


#: Prefix → the predicate that answers "has the condition cleared?". A reason outside this
#: table is prose: unjudgeable here, so it earns an aged reminder rather than a proposal.
_CHECKERS: dict[str, Callable[[str], bool]] = {
    "pr:": _pr_is_settled,
    "issue:": _issue_is_done,
}


def _checker_for(reason: str) -> Callable[[str], bool] | None:
    return next((check for prefix, check in _CHECKERS.items() if reason.startswith(prefix)), None)


@dataclass(slots=True)
class OverrideLiftScanner:
    """Raise one deduped lift question per standing manual override whose reason cleared.

    Hosted on ``housekeeping`` (hourly, non-colleague-facing) because the question is
    about the owner's own factory rather than anyone else's work, so it must keep being
    raised in every posture that runs at all.
    """

    name: str = "override_lift"

    @staticmethod
    def scan() -> list[ScanSignal]:
        proposals = override_lift_proposals()
        raise_override_lift_questions()
        return [
            ScanSignal(
                kind="override.lift_candidate",
                summary=proposal.summary,
                payload={"loop": proposal.loop_name, "runs": proposal.runs, "reason": proposal.reason},
            )
            for proposal in proposals
        ]


__all__ = [
    "PROSE_REMINDER_AGE",
    "OverrideLiftScanner",
    "OverrideProposal",
    "override_lift_proposals",
    "raise_override_lift_questions",
]
