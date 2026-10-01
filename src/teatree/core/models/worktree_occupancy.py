"""The ``Worktree``-row compare-and-swap behind the occupancy claim (#3952, #4867).

Split out of ``teatree.core.worktree.occupancy`` (the ticket-level orchestration —
``occupy_ticket_checkout``, ``refuse_if_ticket_checkout_occupied``): that module lives
under ``teatree.core``, which ``teatree.core.models`` may never import (``tach``'s DAG
would otherwise gain a cycle, since this claim is itself a ``Worktree``/``Task`` field
mutation that already depends on ``core.models``). A holder's own terminal write —
``task_claim.complete_claimed()`` or ``task_claim.fail(by_holder=True)`` — releases its claim
in the SAME transaction as the status write, and a third-party ``task_claim.fail`` releases
only once the holder is confirmed dead; only ``core.models`` can host that without
crossing the boundary. ``core.worktree.occupancy`` imports the primitives back from here
(an allowed ``core`` → ``core.models`` edge) for its ticket-level checkout orchestration.

ONE conditional ``UPDATE ... WHERE <grantable>`` whose affected-row count IS the
decision — never a read-then-write, since teatree's production DB is SQLite, where
``select_for_update`` is a silent no-op. Identity is the FULLY-QUALIFIED ``(holder,
holder_session)`` pair everywhere; ``holder`` alone is never matched.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from teatree.core.claim_liveness import OWNER_COLUMNS, ClaimOwner, holder_confirmed_dead
from teatree.core.models.task import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.worktree import Worktree

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


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

    Raised by ``renew_ticket_checkout`` when a rival now holds the row. The
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
    return f"task:{task.pk}"


def terminal_task_pk(holder: str) -> int | None:
    """The exact inverse of :func:`task_holder_id`, iff that ``Task`` is terminal AND its claim is safe to release.

    ``None`` for a non-``task:`` holder (an operator's ``workspace ticket`` claim
    carries a free-form id), a malformed suffix, a Task that is still active, or a
    terminal Task whose recorded owner may still be live — a third-party
    ``task_claim.fail`` keeps a live holder's claim (#4872), so terminal status alone
    is no proof. Safe means no ``owner_pid`` is recorded (a legacy row, or
    ``reap_stale_claims``' direct CAS, which leaves the Worktree side to this
    self-heal) or :func:`~teatree.core.claim_liveness.holder_confirmed_dead` proves
    the owner gone. Reads the status fresh from the DB, preserving the occupancy
    module's 'advisory, only advisory' invariant.

    A release needs this liveness gate; a park never releases, so it uses
    :func:`terminal_holder_task_pk`. The only caller is the RELEASING self-heal,
    ``core.worktree.occupancy._release_if_finished_task``.
    """
    prefix = "task:"
    suffix = holder.removeprefix(prefix)
    if suffix == holder or not suffix.isdigit():
        return None
    pk = int(suffix)
    row = Task.objects.filter(pk=pk, status__in=Task.Status.terminal()).values("pk", *OWNER_COLUMNS).first()
    if row is None:
        return None
    if row["owner_pid"] is None:
        return pk
    owner = ClaimOwner(
        owner_pid=row["owner_pid"],
        owner_pid_namespace=row["owner_pid_namespace"] or "",
        owner_driving_since=row["owner_driving_since"],
    )
    return pk if holder_confirmed_dead(owner) else None


def terminal_holder_task_pk(holder: str) -> int | None:
    """The exact inverse of :func:`task_holder_id`, iff that ``Task`` is terminal — status only.

    The lenient sibling of :func:`terminal_task_pk`, for a decision that PARKS a
    refused dispatch. A park never releases the claim it inspects, so it takes no
    liveness gate: even over a live holder whose claim a third-party ``task_claim.fail``
    kept (#4872), the old run's heartbeat abort or its lease lapse frees the checkout
    on its own (#4867). A release needs :func:`terminal_task_pk`.
    """
    prefix = "task:"
    suffix = holder.removeprefix(prefix)
    if suffix == holder or not suffix.isdigit():
        return None
    pk = int(suffix)
    return pk if Task.objects.filter(pk=pk, status__in=Task.Status.terminal()).exists() else None


#: The claim TTL. 30 minutes is 30x the 60s run heartbeat that renews it, so no live
#: agent can lose a claim it is still holding; short enough that a crashed operator lane
#: does not hold a checkout for a working day.
_OCCUPANCY_LEASE_SECONDS = 30 * 60


def occupancy_holder(worktree: Worktree) -> OccupancyHolder | None:
    """Who currently holds *worktree*, or ``None`` when it is unheld or the lease lapsed.

    The exact complement of :func:`acquire`'s ``grantable`` predicate, including on a
    row naming a holder with NO expiry: the CAS grants that row, so reporting it as
    held would refuse ``workspace ticket`` forever over a checkout every acquire wins.
    Two predicates for one question is how a lockout gets in — they are complements or
    the gate is incoherent, pinned by ``LivenessAgreementTests``.
    """
    expires = worktree.occupancy_expires_at
    if not worktree.occupied_by or expires is None or expires <= timezone.now():
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

    On a loss the row is read back ONLY to name the incumbent in the refusal; the
    decision was already made by the row count, never by the read. On a win the
    claim just written is returned, so a caller reporting it needs no re-read and
    no "or nobody" fallback for a row it has this instant proven it holds.
    """
    now = timezone.now()
    ttl = _OCCUPANCY_LEASE_SECONDS if lease_seconds is None else lease_seconds
    expires = now + timedelta(seconds=ttl)
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


def release_task_occupancy(task: Task) -> bool:
    """Release *task*'s occupancy claim on its ticket's checkout, iff it holds one (#4867).

    Runs in the SAME transaction as the terminal status write, so a process kill
    before ``occupy_ticket_checkout``'s ``finally`` cannot strand the claim. The
    callers are a holder's own report (``complete_claimed()``, ``fail(by_holder=True)``)
    and a third-party ``fail`` of a confirmed-dead holder; any other third-party fail
    keeps the claim for the holder's own unwind (#4872).
    Callers MUST run this BEFORE blanking ``claimed_by_session``: :func:`release`'s
    CAS matches on that exact session value, so calling this after clearing the claim
    would silently no-op.
    """
    path = dispatch_worktree_path(task.ticket)
    worktree = _worktree_at(task.ticket, path) if path else None
    if worktree is None:
        return False
    return release(worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)


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
