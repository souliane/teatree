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
the previous holder discovers the loss on its own :func:`renew` and aborts, the
same shape ``Task.renew_lease`` has today.

Identity is the FULLY-QUALIFIED ``(holder, holder_session)`` pair everywhere —
CAS predicate, release, renewal, reporting. ``holder`` alone is never matched: two
runs of one task in different sessions are genuinely different occupants, and
matching the bare id would let a stale sibling refresh a claim it no longer owns.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from teatree.core.models import Worktree
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


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

    Raised by :func:`renew` when the claim generation no longer matches. The
    caller must ABORT its work in that checkout rather than keep writing: the
    whole point of the CAS is that two drivers never share one working tree.
    """


@dataclass(frozen=True)
class OccupancyHolder:
    """Who holds a checkout, and until when."""

    holder: str
    holder_session: str
    since: datetime | None
    expires_at: datetime | None

    def describe(self) -> str:
        session = f" (session {self.holder_session})" if self.holder_session else ""
        since = f", held since {self.since.isoformat()}" if self.since else ""
        expiry = f", lease expires {self.expires_at.isoformat()}" if self.expires_at else ""
        return f"{self.holder}{session}{since}{expiry}"


def task_holder_id(task: "Task") -> str:
    """The holder id a dispatched agent occupies a checkout under.

    One function so the acquire, the heartbeat renewal and the release can never
    disagree about who this run is. Namespaced (``task:<pk>``) because a ``Task``
    pk and a forge id both number from ~1.
    """
    return f"task:{task.pk}"


def _default_lease_seconds() -> int:
    """The occupancy TTL, from the DB-home ``worktree_occupancy_lease_seconds`` setting."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return int(get_effective_settings().worktree_occupancy_lease_seconds)


def _gate_enabled() -> bool:
    """Whether the occupancy gate refuses a second requester (the never-lockout kill switch)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return bool(get_effective_settings().worktree_occupancy_gate_enabled)


def occupancy_holder(worktree: Worktree) -> OccupancyHolder | None:
    """Who currently holds *worktree*, or ``None`` when it is unheld or the lease lapsed."""
    if not worktree.occupied_by:
        return None
    expires = worktree.occupancy_expires_at
    if expires is not None and expires <= timezone.now():
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
) -> None:
    """Claim *worktree* for ``(holder, holder_session)``, or refuse naming the incumbent.

    The single conditional ``UPDATE``'s affected-row count is the decision. A row
    is grantable when it is unheld, when its lease has lapsed, or when this exact
    ``(holder, holder_session)`` already holds it — the last making a re-acquire
    idempotent, so a dispatch that resolves its checkout twice refreshes rather
    than deadlocks against itself.

    On a loss the row is read back ONLY to name the incumbent in the refusal; the
    decision was already made by the row count, never by the read.
    """
    now = timezone.now()
    ttl = _default_lease_seconds() if lease_seconds is None else lease_seconds
    grantable = (
        Q(occupied_by="")
        | Q(occupancy_expires_at__isnull=True)
        | Q(occupancy_expires_at__lte=now)
        | Q(occupied_by=holder, occupied_by_session=holder_session)
    )
    won = (
        Worktree.objects.filter(pk=worktree.pk)
        .filter(grantable)
        .update(
            occupied_by=holder,
            occupied_by_session=holder_session,
            occupied_at=now,
            occupancy_expires_at=now + timedelta(seconds=ttl),
        )
    )
    if won != 1:
        raise _occupied_error(worktree)
    worktree.refresh_from_db()


def renew(worktree: Worktree, *, lease_seconds: int | None = None) -> None:
    """Heartbeat this holder's claim — a CAS on the claim generation, not a blind write.

    The predicate is the ``(occupied_by, occupied_by_session, occupied_at)`` the
    caller took the claim under. ``occupied_at`` is re-stamped on every acquire, so
    once the lease lapsed and a rival took over, this holder's predicate matches
    zero rows and must NOT re-stamp the expiry — an unconditional write there
    resurrects a dead claim and puts two agents back in one tree.
    """
    now = timezone.now()
    ttl = _default_lease_seconds() if lease_seconds is None else lease_seconds
    expires = now + timedelta(seconds=ttl)
    renewed = (
        Worktree.objects.filter(pk=worktree.pk)
        .filter(
            occupied_by=worktree.occupied_by,
            occupied_by_session=worktree.occupied_by_session,
            occupied_at=worktree.occupied_at,
        )
        .exclude(occupied_by="")
        .update(occupancy_expires_at=expires)
    )
    if renewed != 1:
        msg = (
            f"occupancy lost for worktree {worktree.pk} at {worktree.worktree_path or '<unprovisioned>'}: "
            "the claim generation moved on (the lease lapsed and another agent took the checkout)"
        )
        raise WorktreeOccupancyLostError(msg)
    worktree.occupancy_expires_at = expires


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

    Re-runs :func:`acquire`, which is idempotent for the same ``(holder,
    holder_session)`` and REPAIRS a claim that lapsed while still unclaimed —
    a starved heartbeat that let its own TTL slip must re-take the tree it is
    still writing to, not leave it advertised as free. A rival holding the
    checkout is the loss the caller has to abort on.

    Renews nothing when the ticket has no materialised checkout or when the gate
    is off: the heartbeat must never mint a claim the dispatch itself did not take.
    """
    if not _gate_enabled():
        return
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        return
    try:
        acquire(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)
    except WorktreeOccupiedError as exc:
        raise WorktreeOccupancyLostError(str(exc)) from exc


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
        f"Checkout {path} is already occupied by {who}. Two agents in one working tree interleave "
        "commits and stage each other's in-progress files, so this request is refused rather than "
        "silently sharing it. Wait for the holder to finish, work a different ticket, or — once you "
        f"have CONFIRMED the holder is gone — hand it back with `t3 <overlay> worktree "
        f"release-occupancy {path}`. Nothing is evicted or deleted on your behalf."
    )
    return WorktreeOccupiedError(msg, holder=holder)
