"""The dream-gap ledger on ``Ticket.extra`` — gaps a pass collected, and what each host did with them.

A dream pass mints no ticket. Its collected gaps queue on the umbrella host's
``dream_gap_pending`` (the ticket whose ``issue_url`` is ``dream_umbrella_url``) until the
backlog sweep folds each into an existing host, which then carries it in
``dream_gap_batch``. A host records, per folded gap, either an ADDRESS (with the citation
that verifies it) or a reasoned REJECT in ``dream_gap_dispositions``; nothing here retires a
memory row.

Every write goes through ``Ticket.merge_extra``'s locked re-read, so a concurrent writer's
keys survive.
"""

from typing import NoReturn, cast

from django.db import transaction

from teatree.config import get_effective_settings
from teatree.core.models.consolidated_memory import ConsolidatedMemory
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import DreamGapEntry

PENDING_KEY = "dream_gap_pending"
BATCH_KEY = "dream_gap_batch"
DISPOSITIONS_KEY = "dream_gap_dispositions"
CLAIMED_DELIVERED_KEY = "dream_gap_claimed_delivered"

ADDRESS = "address"
REJECT = "reject"


class DreamGapLedgerError(ValueError):
    """A disposition the ledger refuses: an unknown gap, or not exactly one of citation / rejection."""


def dream_umbrella_url() -> str:
    return get_effective_settings().dream_umbrella_url


def umbrella_ticket(umbrella_url: str) -> Ticket | None:
    return Ticket.objects.filter(issue_url=umbrella_url).first()


def pending_entries(ticket: Ticket) -> list[DreamGapEntry]:
    return [entry for entry in (ticket.extra or {}).get(PENDING_KEY) or [] if isinstance(entry, dict)]


def queue_pending(ticket: Ticket, entries: list[DreamGapEntry]) -> None:
    queued = {entry.get("gap_key") for entry in pending_entries(ticket)}
    fresh = [entry for entry in entries if entry.get("gap_key") not in queued]
    if fresh:
        ticket.merge_extra(append_to_lists={PENDING_KEY: list(fresh)})


def take_pending(ticket: Ticket, gap_keys: set[str]) -> list[DreamGapEntry]:
    """Remove *gap_keys* from *ticket*'s pending ledger under its row lock; return what was there."""
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        pending = pending_entries(locked)
        taken = [entry for entry in pending if entry.get("gap_key") in gap_keys]
        if taken:
            ticket.merge_extra(set_keys={PENDING_KEY: [entry for entry in pending if entry not in taken]})
    return taken


def batch_entries(ticket: Ticket) -> list[dict[str, str]]:
    return [entry for entry in (ticket.extra or {}).get(BATCH_KEY) or [] if isinstance(entry, dict)]


def undispositioned_gap_keys(ticket: Ticket) -> list[str]:
    dispositions = (ticket.extra or {}).get(DISPOSITIONS_KEY) or {}
    return [entry["gap_key"] for entry in batch_entries(ticket) if entry.get("gap_key") not in dispositions]


def record_gap_disposition(ticket: Ticket, gap_key: str, *, citation: str = "", rejection: str = "") -> str:
    """Record *gap_key*'s ADDRESS (verified by *citation*) or REJECT (*rejection*) on its host; return which.

    An address advances the gap's CANDIDATE memory row to VERIFIED and joins the delivered
    subset the batch reconcile checks off on merge.
    """
    cited, rejected = citation.strip(), rejection.strip()
    if bool(cited) == bool(rejected):
        msg = "a disposition needs exactly one of a citation (address) or a rejection reason (reject)"
        raise DreamGapLedgerError(msg)
    entry = next((entry for entry in batch_entries(ticket) if entry.get("gap_key") == gap_key), None)
    if entry is None:
        msg = f"gap {gap_key!r} is not folded into ticket {ticket.pk}"
        raise DreamGapLedgerError(msg)
    if rejected:
        ticket.merge_extra(
            merge_into_dicts={DISPOSITIONS_KEY: {gap_key: {"disposition": REJECT, "evidence": rejected}}}
        )
        return REJECT
    with transaction.atomic():
        row = ConsolidatedMemory.objects.filter(cluster_key=entry.get("cluster_key") or gap_key).first()
        if row is not None and row.status == ConsolidatedMemory.Status.CANDIDATE:
            row.mark_verified(cited)
        ticket.merge_extra(
            merge_into_dicts={DISPOSITIONS_KEY: {gap_key: {"disposition": ADDRESS, "evidence": cited}}},
            append_to_lists={CLAIMED_DELIVERED_KEY: [gap_key]},
        )
    return ADDRESS


def _refuse_return(member: Ticket, reason: str) -> NoReturn:
    message = f"ticket {member.pk} {reason}"
    raise DreamGapLedgerError(message)


def _return_batch(member: Ticket) -> tuple[str, list[str], list[dict[str, str]]]:
    base, sep, fragment = member.issue_url.partition("#")
    fragment_key, equals, fragment_value = fragment.partition("=")
    entries = batch_entries(member)
    keys = [entry.get("gap_key", "") for entry in entries]
    if base != dream_umbrella_url() or not sep or not fragment_key or not equals or not fragment_value:
        _refuse_return(member, "is not anchored on a dream-gap member")
    if not keys or any(not key for key in keys):
        _refuse_return(member, "has an incomplete gap batch")
    if len(keys) != len(set(keys)):
        _refuse_return(member, "has duplicate gap keys")
    return base, keys, entries


def _require_returnable(member: Ticket, keys: list[str], *, advanced: bool) -> None:
    if member.state in Ticket.finished_states() or (member.state not in Ticket.EARLY_STATES and not advanced):
        _refuse_return(member, "needs --advanced or is already finished")
    if member.tasks.filter(status__in=Task.Status.active()).exists():
        _refuse_return(member, "has a live task")
    if PullRequest.objects.filter(ticket=member).exists():
        _refuse_return(member, "has a pull request")
    if any(not worktree.is_stale for worktree in member.worktrees.all()):
        _refuse_return(member, "has an existing worktree")
    if (member.extra or {}).get(DISPOSITIONS_KEY):
        _refuse_return(member, "has recorded gap dispositions")
    claimed = (member.extra or {}).get(CLAIMED_DELIVERED_KEY) or []
    if not isinstance(claimed, list) or any(not isinstance(key, str) or key not in keys for key in claimed):
        _refuse_return(member, "claims keys outside its gap batch")


def return_member_gaps(member: Ticket, *, advanced: bool = False) -> list[str]:
    with transaction.atomic():
        member = Ticket.objects.select_for_update().get(pk=member.pk)
        base, keys, entries = _return_batch(member)
        umbrella = Ticket.objects.select_for_update().filter(issue_url=base).first()
        if umbrella is None:
            _refuse_return(member, "has no umbrella ticket")
        if member.state == Ticket.State.IGNORED:
            if set(keys) <= {entry.get("gap_key") for entry in pending_entries(umbrella)}:
                return keys
            _refuse_return(member, "is ignored but its gaps are not pending")
        _require_returnable(member, keys, advanced=advanced)
        returned = [
            cast(
                "DreamGapEntry",
                {
                    **entry,
                    "detail": "\n".join(filter(None, [entry.get("detail"), f"Returned from ticket {member.pk}"])),
                },
            )
            for entry in entries
        ]
        queue_pending(umbrella, returned)
        umbrella.refresh_from_db(fields=["extra"])
        if not set(keys) <= {entry.get("gap_key") for entry in pending_entries(umbrella)}:
            _refuse_return(member, "gaps were not recorded on the umbrella")
        member.merge_extra(set_keys={CLAIMED_DELIVERED_KEY: []})
        member.ignore()
        member.save()
        return keys
