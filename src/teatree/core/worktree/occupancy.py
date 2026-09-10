"""Advisory occupancy claim over a checkout — one agent at a time (#3952).

#3903 deduped the ``Task`` seam: two coding Tasks can no longer exist for one
ticket+phase. That does not stop two agents that already HOLD checkouts, and the
autonomous posture makes exactly that routine — the loop mints its own work while
operator-dispatched lanes are live, and both resolve one ticket to one worktree
path. Observed on a single branch in one window: a factory agent committed the
other agent's uncommitted edits under its own commit, a ``git add -A`` staged the
other's in-progress files, and an unpushed local merge was left behind mid-
verification. The trees happened to agree; two agents editing one file would have
lost work silently.

The worktree is the contended resource, so the claim lives on the ``Worktree``
row. :func:`acquire` is the #786 compare-and-swap the ``Task`` lease already uses
(``core.models.task_claim``): ONE conditional ``UPDATE ... WHERE <grantable>``
whose affected-row count IS the decision. Not a read-then-write — teatree's
production DB is SQLite, where ``select_for_update`` is a silent no-op, so two
requesters reading the same unheld row would both write and both believe they own
the checkout.

**Advisory, and only advisory.** A refused requester is TOLD who holds the
checkout; nothing here deletes, evicts, reaps, kills or force-releases anything,
and no other code path is given a deletion signal to read. That constraint is the
ticket's, and it is not stylistic: this repo has already reaped live work by
inferring absence from one execution context. A lapsed lease therefore grants the
NEXT requester without touching the previous holder's process, files or branch —
the previous holder discovers the loss on its own :func:`renew_ticket_checkout`
and aborts, the same shape ``Task.renew_lease`` has today.

Identity is the FULLY-QUALIFIED ``(holder, holder_session)`` pair everywhere —
CAS predicate, release, renewal, reporting. ``holder`` alone is never matched: two
runs of one task in different sessions are genuinely different occupants, and
matching the bare id would let a stale sibling refresh a claim it no longer owns.

A claim outlives its holder in two ways, and both are closed here (#4742). The
heartbeat renews through :func:`extend`, a holder-scoped CAS that can only push out
a lease this pair still owns — re-running :func:`acquire` instead GRANTED the unheld
row whenever the renewal raced its own run's release, re-minting a full-TTL claim
with no live holder left to hand it back. And a claim whose ``task:<pk>`` holder the
DB says has FINISHED is reclaimed on the next :func:`acquire` rather than waiting out
the TTL, which is the surviving case when a worker dies before releasing. Neither
touches the previous holder's process, files or branch: a terminal task is finished
by the DB's own record, so this stays the advisory guard it has always been.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from teatree.core.models import Task, Worktree
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

#: Greppable prefix on the acquire refusal, so ``classify_failure`` names it from a stable
#: token rather than the prose. Kept in step with ``task_failure_taxonomy._MATCHERS`` by
#: ``tests/teatree_core/modelkit/test_task_failure_taxonomy.py``.
CHECKOUT_OCCUPIED_PREFIX = "checkout_occupied: "

_TASK_HOLDER_PREFIX = "task:"


class WorktreeOccupiedError(RuntimeError):
    """A live agent already holds the checkout the caller asked for.

    Carries the :class:`OccupancyHolder` so a caller can route on the holder
    rather than re-parse the message — the dispatch lane records it verbatim on
    the failed attempt, and the CLI prints it.
    """

    def __init__(self, message: str, *, holder: "OccupancyHolder | None" = None) -> None:
        super().__init__(message)
        self.holder = holder


class WorktreeOccupancyLostError(RuntimeError):
    """This holder's claim moved on — the checkout may now be occupied by someone else.

    Raised by :func:`renew_ticket_checkout` when a rival now holds the row. The
    caller must ABORT its work in that checkout rather than keep writing: the
    whole point of the CAS is that two drivers never share one working tree.
    """


@dataclass(frozen=True)
class OccupancyHolder:
    """Who holds a checkout, and until when.

    Only ever describes a LIVE claim, so ``expires_at`` is never absent — an
    unexpiring claim is not held (see :func:`occupancy_holder`).
    """

    holder: str
    holder_session: str
    since: datetime | None
    expires_at: datetime

    def describe(self) -> str:
        session = f" (session {self.holder_session})" if self.holder_session else ""
        since = f", held since {self.since.isoformat()}" if self.since else ""
        return f"{self.holder}{session}{since}, lease expires {self.expires_at.isoformat()}"


def task_holder_id(task: Task) -> str:
    """The holder id a dispatched agent occupies a checkout under.

    One function so the acquire, the heartbeat renewal and the release can never
    disagree about who this run is. Namespaced (``task:<pk>``) because a ``Task``
    pk and a forge id both number from ~1.
    """
    return f"{_TASK_HOLDER_PREFIX}{task.pk}"


def _default_lease_seconds() -> int:
    """The occupancy TTL, from the DB-home ``worktree_occupancy_lease_seconds`` setting."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return int(get_effective_settings().worktree_occupancy_lease_seconds)


def _gate_enabled() -> bool:
    """Whether the occupancy gate refuses a second requester (the never-lockout kill switch)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return bool(get_effective_settings().worktree_occupancy_gate_enabled)


def occupancy_holder(worktree: Worktree) -> OccupancyHolder | None:
    """Who currently holds *worktree*, or ``None`` when nothing live does.

    The exact complement of :func:`acquire`'s ``grantable`` predicate on every row
    state — unheld, lapsed, no expiry at all, and a claim whose holder task has
    already finished. The CAS grants each of those, so reporting one as held would
    refuse ``workspace ticket`` forever over a checkout every acquire wins. Two
    predicates for one question is how a lockout gets in — they are complements or
    the gate is incoherent, pinned by ``LivenessAgreementTests``.
    """
    expires = worktree.occupancy_expires_at
    if not worktree.occupied_by or expires is None or expires <= timezone.now():
        return None
    if _holder_task_finished(worktree.occupied_by):
        return None
    return OccupancyHolder(
        holder=worktree.occupied_by,
        holder_session=worktree.occupied_by_session,
        since=worktree.occupied_at,
        expires_at=expires,
    )


def acquire(
    worktree: Worktree,
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
) -> OccupancyHolder:
    """Claim *worktree* for ``(holder, holder_session)``, or refuse naming the incumbent.

    The single conditional ``UPDATE``'s affected-row count is the decision. A row
    is grantable when it is unheld, when its lease has lapsed, or when this exact
    ``(holder, holder_session)`` already holds it — the last making a re-acquire
    idempotent, so a dispatch that resolves its checkout twice refreshes rather
    than deadlocks against itself.

    A row still naming a holder whose own task has FINISHED is grantable too (#4742):
    the follow-on phase was refused its predecessor's checkout for the rest of the TTL,
    with no live agent anywhere in the tree. That disjunct is holder-scoped, so a rival
    that took the row between the liveness read and this write matches nothing and the
    requester is refused rather than stealing it.

    On a loss the row is read back ONLY to name the incumbent in the refusal; the
    decision was already made by the row count, never by the read. On a win the
    claim just written is returned, so a caller reporting it needs no re-read and
    no "or nobody" fallback for a row it has this instant proven it holds.
    """
    now = timezone.now()
    ttl = _default_lease_seconds() if lease_seconds is None else lease_seconds
    expires = now + timedelta(seconds=ttl)
    grantable = (
        Q(occupied_by="")
        | Q(occupancy_expires_at__isnull=True)
        | Q(occupancy_expires_at__lte=now)
        | Q(occupied_by=holder, occupied_by_session=holder_session)
    )
    finished = _finished_holder(worktree)
    if finished is not None:
        grantable |= Q(occupied_by=finished[0], occupied_by_session=finished[1])
    won = (
        Worktree.objects.filter(pk=worktree.pk)
        .filter(grantable)
        .update(
            occupied_by=holder,
            occupied_by_session=holder_session,
            occupied_at=now,
            occupancy_expires_at=expires,
        )
    )
    if won != 1:
        raise _occupied_error(worktree)
    worktree.refresh_from_db()
    return OccupancyHolder(holder=holder, holder_session=holder_session, since=now, expires_at=expires)


def release(worktree: Worktree, *, holder: str, holder_session: str = "") -> bool:
    """Hand *worktree* back, iff ``(holder, holder_session)`` is the current occupant.

    Holder-scoped by CAS so a release can never steal: a caller that no longer
    owns the claim (or never did) updates zero rows and gets ``False``. Returns
    whether this call is what freed it.
    """
    freed = (
        Worktree.objects.filter(pk=worktree.pk, occupied_by=holder, occupied_by_session=holder_session)
        .exclude(occupied_by="")
        .update(occupied_by="", occupied_by_session="", occupied_at=None, occupancy_expires_at=None)
    )
    if freed == 1:
        worktree.refresh_from_db()
    return freed == 1


def extend(
    worktree: Worktree,
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
) -> OccupancyHolder:
    """Push THIS holder's lease out, or report that the claim moved on.

    Holder-scoped by CAS, so a renewal can only ever refresh a claim
    ``(holder, holder_session)`` still owns — never mint one. Re-running
    :func:`acquire` here instead granted the row whenever a renewal raced its own
    run's release, leaving a full-TTL claim behind every completed run (#4742).

    ``occupied_at`` is deliberately not rewritten: held-since must stay the instant the
    checkout was taken, or the report reads as if each heartbeat were a fresh claim.
    """
    now = timezone.now()
    ttl = _default_lease_seconds() if lease_seconds is None else lease_seconds
    expires = now + timedelta(seconds=ttl)
    extended = (
        Worktree.objects.filter(pk=worktree.pk, occupied_by=holder, occupied_by_session=holder_session)
        .exclude(occupied_by="")
        .update(occupancy_expires_at=expires)
    )
    if extended != 1:
        raise WorktreeOccupancyLostError(_claim_moved_on(worktree, holder=holder))
    worktree.refresh_from_db()
    return OccupancyHolder(
        holder=holder,
        holder_session=holder_session,
        since=worktree.occupied_at,
        expires_at=expires,
    )


@contextmanager
def occupy_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
    enabled: bool | None = None,
) -> Iterator[str]:
    """Hold *ticket*'s dispatch checkout for the duration of the block.

    Yields the on-disk path an agent should run in, and releases the claim on the
    way out — including when the body raises, so a crashed run frees the checkout
    at once instead of making the next requester wait out the TTL.

    Yields ``""`` and claims nothing when the ticket has no materialised checkout:
    there is no shared resource yet, so there is nothing to contend on and a
    pre-provision dispatch behaves exactly as it does today. Same when the gate is
    switched off — the kill switch hands the path out ungated rather than
    pretending the claim succeeded.
    """
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None or not (_gate_enabled() if enabled is None else enabled):
        yield path
        return

    acquire(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)
    try:
        yield path
    finally:
        release(worktree, holder=holder, holder_session=holder_session)


def renew_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
) -> None:
    """Heartbeat this holder's claim on *ticket*'s checkout, or report that it moved on.

    Runs :func:`extend`, which REPAIRS a claim that lapsed while the row still names
    this holder — a starved heartbeat that let its own TTL slip must re-take the tree
    it is still writing to, not leave it advertised as free — while refusing to mint
    one over a row this pair no longer holds. A rival on the checkout, and a claim
    this run's own release already handed back, are both the loss the caller aborts on.

    Renews nothing when the ticket has no materialised checkout or when the gate
    is off: the heartbeat must never mint a claim the dispatch itself did not take.
    """
    if not _gate_enabled():
        return
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        return
    extend(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)


def refuse_if_ticket_checkout_occupied(ticket: "Ticket") -> None:
    """Refuse when a live agent already occupies *ticket*'s checkout (#3952).

    The read-only half of the chokepoint, for a caller that HANDS BACK a checkout
    without holding it for a bounded run — ``workspace ticket`` re-resolving a
    ticket whose worktree already exists. It takes no claim of its own (the
    command exits, so a claim it took would outlive it as a phantom holder) and
    it changes nothing: the whole effect is telling the second requester who is
    already in the tree instead of pointing them into it.
    """
    if not _gate_enabled():
        return
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is not None and occupancy_holder(worktree) is not None:
        raise _occupied_error(worktree)


def held_worktrees() -> list[tuple[Worktree, OccupancyHolder]]:
    """Every worktree under a LIVE occupancy claim, for the doctor's report."""
    pairs = (
        (worktree, occupancy_holder(worktree)) for worktree in Worktree.objects.exclude(occupied_by="").order_by("pk")
    )
    return [(worktree, holder) for worktree, holder in pairs if holder is not None]


def _holder_task_finished(holder: str) -> bool:
    """Whether *holder* names a ``task:<pk>`` the DB records as already finished.

    Fails closed on everything it cannot positively prove finished — an operator's
    hand-driven holder, an unparsable id, a row that no longer exists — so a claim is
    only ever handed on when the DB itself says its agent is done.
    """
    raw = holder.removeprefix(_TASK_HOLDER_PREFIX)
    if raw == holder or not raw.isdigit():
        return False
    return Task.objects.filter(pk=int(raw), status__in=Task.Status.terminal()).exists()


def _finished_holder(worktree: Worktree) -> tuple[str, str] | None:
    """The LIVE row's ``(holder, holder_session)`` when its holder task has already finished.

    Read back rather than taken off the caller's instance: a stale in-memory row names
    the wrong occupant, and the CAS disjunct built from it would then match nothing.
    """
    row = Worktree.objects.filter(pk=worktree.pk).values_list("occupied_by", "occupied_by_session").first()
    if row is None or not _holder_task_finished(row[0]):
        return None
    return row


def _claim_moved_on(worktree: Worktree, *, holder: str) -> str:
    """Why a holder-scoped extend matched no row — a rival by name, or the claim simply gone."""
    current = Worktree.objects.filter(pk=worktree.pk).first()
    incumbent = occupancy_holder(current) if current is not None else None
    path = (current or worktree).worktree_path or "<unprovisioned>"
    if incumbent is not None:
        return f"Checkout {path} is already occupied by {incumbent.describe()}, not by {holder}."
    return f"Checkout {path} is no longer held by {holder} — the claim was released or reclaimed."


def _worktree_at(ticket: "Ticket", path: str) -> Worktree | None:
    """The ticket's ``Worktree`` row whose recorded checkout is *path*."""
    return Worktree.objects.filter(ticket=ticket, extra__worktree_path=path).order_by("pk").first()


def _occupied_error(worktree: Worktree) -> WorktreeOccupiedError:
    """The refusal for a lost acquisition, naming the incumbent read back from the row."""
    current = Worktree.objects.filter(pk=worktree.pk).first()
    holder = occupancy_holder(current) if current is not None else None
    path = (current or worktree).worktree_path or "<unprovisioned>"
    who = holder.describe() if holder is not None else "another agent"
    msg = (
        f"{CHECKOUT_OCCUPIED_PREFIX}Checkout {path} is already occupied by {who}. Two agents in one "
        "working tree interleave "
        "commits and stage each other's in-progress files, so this request is refused rather than "
        "silently sharing it. Wait for the holder to finish, work a different ticket, or — once you "
        f"have CONFIRMED the holder is gone — hand it back with `t3 <overlay> worktree "
        f"release-occupancy {path}`. Nothing is evicted or deleted on your behalf."
    )
    return WorktreeOccupiedError(msg, holder=holder)
