"""Doctor gate: enabled loops are configured but nothing is ticking them.

The resource_pressure disk-full incident root cause: ``t3 loop status`` showed
scanners *configured* with intervals (``resource_pressure`` 1m, …) but
``crontab -l`` had NO entry firing ``t3 loop tick`` — so NO scanner ever ran.
The intervals are config consulted by the tick, not a live scheduler; with no
tick runner, resource_pressure never reaped Docker build cache + images and
disk hit 99 %. The gap was *silent* — nothing surfaced "configured but never
ticking". This gate makes that gap loud at ``t3 doctor`` time.

The predicate. An enabled :class:`teatree.core.models.Loop` row is *configured*
work; a live tick runner is what *drives* it (BLUEPRINT § 5.6 invariant 2: a
machine-wide tick driven by the recurring ``t3 loop tick`` cron, or pumped by a
live owning session). A runner is live when either the persistent
``loop-owner`` lease names a live session (pid alive or TTL unexpired) OR the
per-tick ``loop-tick`` lease shows a *recent* acquisition (a cron just ticked).
Enabled rows + no live runner ⇒ unhealthy (the incident state). No enabled rows
⇒ healthy (the #2513 cutover ships every row PAUSED — that install drives
nothing on purpose, not a defect).
"""

import datetime as dt
import logging
from dataclasses import dataclass

import typer
from django.utils import timezone

from teatree.utils.singleton import pid_alive

logger = logging.getLogger(__name__)

# Well-known lease names (mirrors ``teatree.loops.live`` constants without the
# backwards import: this gate is ``teatree.core`` domain, ``teatree.loops`` is
# orchestration). The persistent session claim and the per-tick mutex.
OWNER_SLOT = "loop-owner"
TICK_SLOT = "loop-tick"

# A ``loop-tick`` acquisition older than this with no live owner is treated as
# "the cron has stopped firing" — generous enough that a normal multi-minute
# tick cadence never trips it, tight enough that a dead scheduler is caught.
_TICK_STALE_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class LoopRunnerHealth:
    """Whether enabled loops have a live tick runner driving them."""

    healthy: bool
    enabled_loop_count: int
    enabled_loop_names: tuple[str, ...]
    has_live_owner: bool
    last_tick_age_seconds: float | None

    def render_fail(self) -> str:
        """The ``t3 doctor`` FAIL line — names the enabled-but-undriven loops."""
        names = ", ".join(self.enabled_loop_names) or "(none named)"
        tick = (
            "the loop has never ticked"
            if self.last_tick_age_seconds is None
            else f"the last tick was {int(self.last_tick_age_seconds)}s ago"
        )
        return (
            f"FAIL  {self.enabled_loop_count} loop(s) enabled ({names}) but no live tick runner — "
            f"{tick} and no live `loop-owner` session. Enabled loops are CONFIG, not a scheduler: "
            f"with nothing firing `t3 loop tick`, scanners like resource_pressure never run "
            f"(this is how disk filled to 99% unreaped). Register the `t3 loop tick` cron, or "
            f"start a session that claims the loop (`t3 loop claim`), then re-run `t3 doctor check`."
        )


def _has_live_owner(now: dt.datetime) -> bool:
    """True iff the persistent ``loop-owner`` lease names a live session.

    Liveness is PID-anchored with a TTL fallback (mirrors
    :class:`teatree.core.models.loop_lease.LoopLease`): a non-empty owner whose
    ``owner_pid`` is alive, OR whose lease TTL has not yet expired, is a live
    runner. An empty/dead/lapsed owner is not.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415

    lease = LoopLease.objects.filter(name=OWNER_SLOT).first()
    if lease is None or not lease.session_id:
        return False
    pid_ok = lease.owner_pid is not None and pid_alive(lease.owner_pid)
    ttl_live = lease.lease_expires_at is not None and lease.lease_expires_at > now
    return pid_ok or ttl_live


def _last_tick_age_seconds(now: dt.datetime) -> float | None:
    """Seconds since the ``loop-tick`` lease was last acquired, ``None`` if never."""
    from teatree.core.models import LoopLease  # noqa: PLC0415

    lease = LoopLease.objects.filter(name=TICK_SLOT).first()
    if lease is None or lease.acquired_at is None:
        return None
    return (now - lease.acquired_at).total_seconds()


def loop_runner_health(*, now: dt.datetime | None = None) -> LoopRunnerHealth:
    """Resolve whether enabled loops have a live tick runner driving them.

    Read-only: enumerates the enabled ``Loop`` rows, then probes the live tick
    runner via the ``loop-owner`` / ``loop-tick`` leases. Enabled rows with no
    live runner is the unhealthy 'configured but never ticking' state.
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    moment = now if now is not None else timezone.now()
    enabled_names = tuple(Loop.objects.enabled().order_by("name").values_list("name", flat=True))
    count = len(enabled_names)
    if count == 0:
        return LoopRunnerHealth(
            healthy=True,
            enabled_loop_count=0,
            enabled_loop_names=(),
            has_live_owner=False,
            last_tick_age_seconds=None,
        )
    has_owner = _has_live_owner(moment)
    tick_age = _last_tick_age_seconds(moment)
    tick_recent = tick_age is not None and tick_age <= _TICK_STALE_SECONDS
    healthy = has_owner or tick_recent
    return LoopRunnerHealth(
        healthy=healthy,
        enabled_loop_count=count,
        enabled_loop_names=enabled_names,
        has_live_owner=has_owner,
        last_tick_age_seconds=tick_age,
    )


def doctor_check_loop_tick_runner() -> bool:
    """``t3 doctor`` surface for the loop-tick-runner gate (resource_pressure RETRO).

    Returns ``True`` (passed) when no loops are enabled, or a live tick runner
    is driving them. Returns ``False`` with a ``FAIL`` line naming the enabled-
    but-undriven loops when loops are configured but nothing ticks them — the
    silent gap that let disk fill to 99 % unreaped.

    Crash-proof: any ORM error (DB absent/offline, unmigrated self-DB) degrades
    to a WARN + pass, mirroring the other DB-reading doctor checks
    (``doctor_check_self_db_migrations``, ``_check_dream_staleness``) — a doctor
    run never aborts on this check.
    """
    try:
        health = loop_runner_health()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Loop-tick-runner check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if health.healthy:
        return True
    typer.echo(health.render_fail())
    return False


__all__ = [
    "LoopRunnerHealth",
    "doctor_check_loop_tick_runner",
    "loop_runner_health",
]
