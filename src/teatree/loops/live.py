"""Live loop-status snapshot shared by the statusline and ``t3 loop list`` (#1744).

``t3 loop status`` prints the statusline file written at the *last* tick, so
its countdowns are stale — it can still show a live-looking loop line while
the loop has been dead for hours. This module computes the same state LIVE from
the DB on every call. The #2513 cutover: the mini-loop rows now come from the
DB ``Loop`` table (each row's ``enabled``/cadence/``last_run_at``/next-due) —
the single source of truth, replacing the legacy ``MiniLoopMarker`` cadence
ledger + ``[loops]``-toml ``LoopsConfig``. Infra-slot leases
(:class:`LoopLease`) with PID-anchored owner liveness are read alongside.

Strictly read-only: it issues ORM reads only — never ticks, claims, acquires,
marks fired, or mutates a row. :func:`teatree.loops.schedule.mini_loop_schedules`
derives its mini-loop next-fire numbers from :func:`build_report` so the
statusline and ``t3 loop list`` never drift.
"""

import datetime as dt
import operator
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.loop_lease_manager import is_per_loop_owner_slot
from teatree.core.models.loop_lease import LoopLease
from teatree.loop.statusline_loops import _cadence_for_loop as cadence_for_loop
from teatree.utils.singleton import pid_alive

if TYPE_CHECKING:
    from teatree.core.models import Loop

INFRA_SLOTS: tuple[str, ...] = (
    "loop-tick",
    "loop-self-improve",
    "loop-slack-answer",
    "loop-drain-queue",
)

OWNER_SLOT = "loop-owner"
TICK_SLOT = "loop-tick"
STALL_FACTOR = 2


class LoopKind(StrEnum):
    INFRA = "infra-slot"
    MINI = "mini-loop"


@dataclass(frozen=True, slots=True)
class LoopOwnerStatus:
    session_id: str
    owner_pid: int | None
    pid_is_alive: bool
    is_live: bool
    slot: str = OWNER_SLOT

    @property
    def is_claimed(self) -> bool:
        return bool(self.session_id)


@dataclass(frozen=True, slots=True)
class LoopStatusEntry:
    name: str
    kind: LoopKind
    enabled: bool
    cadence_seconds: int
    last_fired_at: dt.datetime | None
    next_fire_at: dt.datetime | None
    held: bool = False

    @property
    def never_fired(self) -> bool:
        return self.last_fired_at is None

    def age_seconds(self, now: dt.datetime) -> float | None:
        if self.last_fired_at is None:
            return None
        return (now - self.last_fired_at).total_seconds()

    def overdue(self, now: dt.datetime) -> bool:
        return self.next_fire_at is not None and self.next_fire_at <= now

    def due_seconds(self, now: dt.datetime) -> float | None:
        if self.next_fire_at is None:
            return None
        return (self.next_fire_at - now).total_seconds()


@dataclass(frozen=True, slots=True)
class LoopStatusReport:
    generated_at: dt.datetime
    infra_slots: tuple[LoopStatusEntry, ...]
    mini_loops: tuple[LoopStatusEntry, ...]
    owner: LoopOwnerStatus
    tick_cadence_seconds: int
    last_tick_at: dt.datetime | None
    #: Additive per-loop owning-session layer (#1834). One entry per
    #: ``loop:<name>`` lease row that has ever been claimed — the
    #: cross-session health view shown by ``t3 loop list --all``. Empty
    #: under today's single-owner default (no dedicated loop has claimed a
    #: per-loop slot), so the default ``t3 loop list`` view is byte-identical.
    per_loop_owners: tuple[LoopOwnerStatus, ...] = ()

    @property
    def last_tick_age_seconds(self) -> float | None:
        if self.last_tick_at is None:
            return None
        return (self.generated_at - self.last_tick_at).total_seconds()

    @property
    def stalled(self) -> bool:
        age = self.last_tick_age_seconds
        if age is None:
            return True
        return age > STALL_FACTOR * self.tick_cadence_seconds


def build_report(*, now: dt.datetime | None = None) -> LoopStatusReport:
    moment = now if now is not None else timezone.now()
    leases = {row.name: row for row in LoopLease.objects.all()}
    infra = tuple(_infra_entry(slot, leases.get(slot)) for slot in INFRA_SLOTS)
    mini = _mini_entries()
    owner = _owner_status(leases.get(OWNER_SLOT), moment, slot=OWNER_SLOT)
    per_loop_owners = _per_loop_owners(leases, moment)
    tick_cadence = cadence_for_loop(TICK_SLOT)
    return LoopStatusReport(
        generated_at=moment,
        infra_slots=infra,
        mini_loops=mini,
        owner=owner,
        per_loop_owners=per_loop_owners,
        tick_cadence_seconds=tick_cadence,
        last_tick_at=_last_tick_at(infra, mini),
    )


def _infra_entry(slot: str, lease: LoopLease | None) -> LoopStatusEntry:
    cadence = cadence_for_loop(slot)
    acquired_at = lease.acquired_at if lease is not None else None
    held = lease.is_held if lease is not None else False
    next_fire_at = acquired_at + dt.timedelta(seconds=cadence) if acquired_at is not None else None
    return LoopStatusEntry(
        name=slot,
        kind=LoopKind.INFRA,
        enabled=True,
        cadence_seconds=cadence,
        last_fired_at=acquired_at,
        next_fire_at=next_fire_at,
        held=held,
    )


_DAY_SECONDS = 86400


def _row_cadence_seconds(loop: "Loop") -> int:
    """Resolve a ``Loop`` row's cadence for the status denominator.

    An interval row reports its ``delay_seconds``; a ``daily_at`` row reports the
    day window (it fires once per day on/after the wall-clock time); a row with
    neither (due every tick) reports ``0`` so the renderer treats it as immediate.
    """
    if loop.daily_at is not None:
        return _DAY_SECONDS
    return loop.delay_seconds or 0


def _mini_entries() -> tuple[LoopStatusEntry, ...]:
    """Live mini-loop status from the DB ``Loop`` table (#2513 cutover).

    The cutover SOT: each enabled/disabled state, cadence, last-run anchor, and
    next-due instant comes from the ``Loop`` row (was ``LoopsConfig`` +
    ``MiniLoopMarker``). One read here re-points BOTH the statusline and
    ``t3 loop list`` since both consume :func:`build_report`.
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    entries = [
        LoopStatusEntry(
            name=loop.name,
            kind=LoopKind.MINI,
            enabled=loop.enabled,
            cadence_seconds=_row_cadence_seconds(loop),
            last_fired_at=loop.last_run_at,
            next_fire_at=loop.next_run_at(),
        )
        for loop in Loop.objects.all()
    ]
    return tuple(sorted(entries, key=operator.attrgetter("name")))


def _owner_status(lease: LoopLease | None, now: dt.datetime, *, slot: str) -> LoopOwnerStatus:
    if lease is None or not lease.session_id:
        return LoopOwnerStatus(session_id="", owner_pid=None, pid_is_alive=False, is_live=False, slot=slot)
    pid_ok = lease.owner_pid is not None and pid_alive(lease.owner_pid)
    ttl_live = lease.lease_expires_at is not None and lease.lease_expires_at > now
    return LoopOwnerStatus(
        session_id=lease.session_id,
        owner_pid=lease.owner_pid,
        pid_is_alive=pid_ok,
        is_live=ttl_live or pid_ok,
        slot=slot,
    )


def _per_loop_owners(leases: dict[str, LoopLease], now: dt.datetime) -> tuple[LoopOwnerStatus, ...]:
    """Owner status for every per-loop ``loop:<name>`` lease (#1834).

    Read-only: derives one :class:`LoopOwnerStatus` per per-loop slot row
    present in the DB, sorted by slot for a stable health view. Empty under
    the single-owner default — no dedicated loop has claimed a per-loop slot
    — so the default ``t3 loop list`` (which never reads this) is unchanged.
    """
    per_loop = [_owner_status(lease, now, slot=name) for name, lease in leases.items() if is_per_loop_owner_slot(name)]
    return tuple(sorted(per_loop, key=operator.attrgetter("slot")))


def owned_per_loop_owners(report: LoopStatusReport, session_id: str) -> tuple[LoopOwnerStatus, ...]:
    """Per-loop owners scoped to ``session_id`` — the default-view filter (#1834 WI-2).

    The shared seam both the ``t3 loop list`` default branch and the
    statusline use so the two renderers can never drift: it filters
    :attr:`LoopStatusReport.per_loop_owners` (every ``loop:<name>`` lease,
    the cross-session ``--all`` set) down to the loops owned by
    ``session_id`` via the same :attr:`LoopOwnerStatus.slot` /
    ``session_id`` keys that built the set.

    **Fail-open:** an empty ``session_id`` (a cron / anonymous tick that
    cannot resolve a session) returns the FULL set, never an empty one — the
    default view degrades to the cross-session health view rather than
    hiding live loops. Empty input (the single-owner default, no ``loop:``
    lease) returns ``()`` for any session, so the default view short-circuits
    to today's exact output.
    """
    if not session_id:
        return report.per_loop_owners
    return tuple(owner for owner in report.per_loop_owners if owner.session_id == session_id)


def _last_tick_at(infra: tuple[LoopStatusEntry, ...], mini: tuple[LoopStatusEntry, ...]) -> dt.datetime | None:
    fired = [entry.last_fired_at for entry in (*infra, *mini) if entry.last_fired_at is not None]
    return max(fired) if fired else None


__all__ = [
    "INFRA_SLOTS",
    "OWNER_SLOT",
    "STALL_FACTOR",
    "TICK_SLOT",
    "LoopKind",
    "LoopOwnerStatus",
    "LoopStatusEntry",
    "LoopStatusReport",
    "build_report",
    "owned_per_loop_owners",
]
