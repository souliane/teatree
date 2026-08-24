"""Global operational-health aggregator (PR-17, M6).

Computes a single green / yellow / red verdict for "is the factory healthy right
now" from deterministic durable signals — stale loop ticks, failed tasks,
overlay-declared problems — and persists each as a :class:`KnownIssue` row so
the verdict survives compaction and an operator can see *which* things are
wrong, not just the color.

This is deliberately NOT :mod:`teatree.core.worktree.health` — that module is the
post-provision per-worktree readiness checks (symlinks, env cache). This one is
the global factory-health chip surfaced in the statusline anchors zone, on
``t3 <overlay> health show``, and in the ``/t3:health`` detail skill.

Two entry points, split by side-effect:

*   :func:`reconcile_health` collects every live signal, upserts a
    :class:`KnownIssue` row per signal, and auto-resolves the rows whose signal
    has cleared — the writing path, called from the loop tick and from
    ``health show``. Auto-resolution runs only on a COMPLETE observation: a
    collector that could not READ contributes the same empty slice as one
    reporting "all clear", so absence is evidence of resolution only when every
    source answered (:class:`SignalCollection`, #4354).
*   :func:`read_health` is read-only: it computes the verdict + open-issue set
    from the persisted rows alone, for the statusline chip that renders every
    tick without wanting to write.

Thresholds (spec): red = any critical signal or three-or-more concurrent
yellows; yellow = any non-critical signal; green otherwise.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from teatree.core.loop_lease_manager import T3_MASTER_SLOT, is_per_loop_owner_slot, is_per_loop_tick_mutex
from teatree.core.models.known_issue import KnownIssue
from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.throttled_log import warn_throttled

if TYPE_CHECKING:
    from teatree.core.models.loop_lease import LoopLease
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

# A held loop lease that has not re-acquired within this multiple of its cadence
# has missed enough ticks to count as stale (spec: "overrun > 2x cadence").
_TICK_OVERRUN_MULTIPLE = 2
# Failed tasks older than this are stale audit trail, not a live health signal —
# a single old failure should not keep the chip yellow forever.
_FAILED_TASK_WINDOW = timedelta(hours=6)
# Three concurrent yellows is the red threshold (spec).
_RED_YELLOW_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class HealthSignal:
    """One live "something is wrong" observation feeding the aggregator.

    *fingerprint* is the stable dedupe key — the same problem seen on two ticks
    carries the same fingerprint so it updates one :class:`KnownIssue` row
    rather than piling up duplicates. *severity* is a
    :class:`KnownIssue.Severity` value (``critical`` / ``warning``). *kind* is a
    coarse machine label for the signal family; *overlay* scopes it; *summary*
    is the human line; *evidence_url* is the clickable jump-to-proof link.
    """

    fingerprint: str
    severity: str
    summary: str
    kind: str = ""
    overlay: str = ""
    evidence_url: str = ""


@dataclass(frozen=True, slots=True)
class SignalCollection:
    """What a health read SAW, and which sources it could not read at all (#4354).

    ``signals`` are the live observations. ``unread`` names every source whose read
    FAILED — an overlay that raised, a DB query that errored, a whole collector that
    blew up. The two are kept apart because both a failed read and an all-clear read
    contribute zero signals, and :meth:`KnownIssueManager.reconcile` treats a missing
    fingerprint as RESOLVED: collapsing them retires an issue nothing has fixed.
    """

    signals: tuple[HealthSignal, ...] = ()
    unread: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True iff every source answered, so an absent fingerprint really did clear."""
        return not self.unread


class HealthStatus(StrEnum):
    """The global-health verdict, in ascending severity."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The computed verdict plus the open issues that produced it."""

    status: HealthStatus
    open_issues: tuple[KnownIssue, ...]

    @property
    def open_count(self) -> int:
        return len(self.open_issues)


def _overlay_health_signals() -> SignalCollection:
    """Fold every registered overlay's ``get_health_signals()`` into one collection.

    Each overlay is queried independently and fail-open — one overlay raising
    never suppresses another's signals, so a broken overlay degrades to
    "declares nothing" rather than blanking the whole health surface. It names
    itself in ``unread`` so "declares nothing" is never read as "declares clear".
    """
    signals: list[HealthSignal] = []
    unread: list[str] = []
    for name, overlay in get_all_overlays().items():
        try:
            signals.extend(overlay.get_health_signals())
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
            unread.append(f"overlay:{name}")
            # A one-off miss is expected; a persistently-failing overlay health
            # read is a real fault the chip would otherwise silently drop — surface
            # it at warning, throttled so a per-tick failure is not logged every beat.
            warn_throttled(
                logger,
                f"health-overlay:{name}",
                "overlay %s get_health_signals() failed — skipped",
                name,
                exc_info=True,
            )
    return SignalCollection(tuple(signals), tuple(unread))


def _lease_reference_seconds(name: str) -> int:
    """Seconds a live lease may age before it counts as stale — its OWN cadence/TTL.

    Mirrors the display resolver
    :func:`teatree.loop.statusline_loops._cadence_for_loop`: each infra loop ticks
    on its own schedule, so a single flat cadence over-reports. A reactive slot
    resolves its env cadence; a per-loop owner lease (``loop:<name>``) uses the
    pid-anchored claim TTL it was granted under; everything else (the bare
    ``loop-tick`` mutex, an unknown or newly-added loop) falls back to the
    ``loop-tick`` cadence, so a new loop surfaces without a change here.
    """
    from teatree.config import cadence_seconds  # noqa: PLC0415 — deferred to keep the module cold-import cheap
    from teatree.loop.loop_cadences import (  # noqa: PLC0415 — deferred: pure os.environ readers, the SoT for each loop's cadence
        drain_cadence_seconds,
        loop_owner_ttl_seconds,
        self_improve_cadence_seconds,
        slack_answer_cadence_seconds,
    )

    if name == "loop-self-improve":
        return self_improve_cadence_seconds()
    if name == "loop-slack-answer":
        return slack_answer_cadence_seconds()
    if name == "loop-drain-queue":
        return drain_cadence_seconds()
    if is_per_loop_owner_slot(name):
        return loop_owner_ttl_seconds()
    return cadence_seconds()


def _stale_tick_signals() -> SignalCollection:
    """One warning per cadence-ticked loop lease that has overrun its OWN cadence.

    A held :class:`~teatree.core.models.loop_lease.LoopLease` whose last acquire
    is older than :data:`_TICK_OVERRUN_MULTIPLE` x the loop's OWN cadence/TTL
    (:func:`_lease_reference_seconds`) has not ticked in too long — the loop is
    wedged even though the lease is still nominally live.

    Two leases are excluded, mirroring the display's
    :func:`teatree.loop.statusline_loops._live_lease_chunks`:

    *   ``t3-master`` is a pid-anchored session-ownership token, deliberately
        NOT re-acquired while its owner is BUSY (busy != dead, #1073/#1604).
        During a routine multi-minute busy window its ``acquired_at``
        legitimately ages past any tick cutoff, so judging it as a tick would
        spuriously redden the health chip on a healthy factory.
    *   the transient per-loop tick mutex ``loop-tick:<name>`` (#2650) is a
        concurrency lock held only for the beat, never a user-facing loop.

    Fail-open, naming itself ``unread`` so a lease table it could not query is
    never read as "no loop is wedged".
    """
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred so the app registry is only touched at read time

        now = timezone.now()
        lease_model = cast("type[LoopLease]", apps.get_model("core", "LoopLease"))
        rows = lease_model.objects.filter(
            lease_expires_at__gt=now,
            acquired_at__isnull=False,
        ).only("name", "acquired_at")
        stale = [
            row
            for row in rows
            if row.name != T3_MASTER_SLOT
            and not is_per_loop_tick_mutex(row.name)
            and row.acquired_at < now - timedelta(seconds=_TICK_OVERRUN_MULTIPLE * _lease_reference_seconds(row.name))
        ]
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-stale-tick", "stale-tick health read failed — skipped", exc_info=True)
        return SignalCollection(unread=("_stale_tick_signals",))
    return SignalCollection(
        tuple(
            HealthSignal(
                fingerprint=f"stale-tick:{row.name}",
                severity=KnownIssue.Severity.WARNING,
                kind="stale_tick",
                summary=f"loop {row.name} has not ticked in over {_TICK_OVERRUN_MULTIPLE}x its cadence",
            )
            for row in stale
        )
    )


def _failed_task_signals() -> SignalCollection:
    """One warning summarising recently-failed tasks (spec: failed answering tasks).

    Collapses every :class:`~teatree.core.models.task.Task` that FAILED inside
    :data:`_FAILED_TASK_WINDOW` into a single count so N failures are one chip
    line, not N. Fail-open, naming itself ``unread`` on a read it could not make.
    """
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred so the app registry is only touched at read time

        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        cutoff = timezone.now() - _FAILED_TASK_WINDOW
        count = task_model.objects.filter(status="failed", created_at__gte=cutoff).count()
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-failed-task", "failed-task health read failed — skipped", exc_info=True)
        return SignalCollection(unread=("_failed_task_signals",))
    if count <= 0:
        return SignalCollection()
    noun = "task" if count == 1 else "tasks"
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="failed-tasks",
                severity=KnownIssue.Severity.WARNING,
                kind="failed_tasks",
                summary=f"{count} {noun} failed in the last {int(_FAILED_TASK_WINDOW.total_seconds() // 3600)}h",
            ),
        )
    )


def _harness_provider_consistency_signals() -> SignalCollection:
    """One CRITICAL per scope whose effective (agent_harness, agent_harness_provider) pair is inconsistent (#3688).

    A pair the harness registry would refuse at dispatch — set before the
    write-time guard existed, or via a path the guard does not cover — otherwise
    fails EVERY dispatch in that scope, one repair-halt at a time. Surfacing it as
    a single loud health-red replaces that per-task flood with one visible signal.
    The effective pair is resolved exactly as dispatch resolves it
    (:func:`~teatree.config.get_effective_settings`, env → DB → default) for the
    global/active scope and each registered overlay; an overlay-registered harness
    is unconstrained here (its constraint lives in the open registry). Per-scope
    fail-open so one broken resolve never suppresses another scope's signal.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred to keep the module cold-import cheap
    from teatree.config.cross_key_consistency import (  # noqa: PLC0415 — deferred: same cold-import discipline
        check_harness_provider_pair,
    )

    signals: list[HealthSignal] = []
    unread: list[str] = []
    seen: set[str] = set()
    scopes: list[str | None] = [None, *sorted(get_all_overlays())]
    for scope in scopes:
        try:
            settings = get_effective_settings(scope)
            provider = settings.agent_harness_provider
            reason = check_harness_provider_pair(
                settings.agent_harness,
                provider.value if provider is not None else None,
            )
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
            unread.append(f"harness-provider-pair:{scope or 'global'}")
            warn_throttled(
                logger,
                f"health-harness-pair:{scope or 'global'}",
                "harness/provider consistency health read failed for scope %s — skipped",
                scope or "global",
                exc_info=True,
            )
            continue
        if reason is None:
            continue
        label = scope or "global"
        fingerprint = f"harness-provider-drift:{label}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        signals.append(
            HealthSignal(
                fingerprint=fingerprint,
                severity=KnownIssue.Severity.CRITICAL,
                kind="config_pair_drift",
                overlay=scope or "",
                summary=f"agent_harness/agent_harness_provider mismatch [{label}]: {reason}",
            ),
        )
    return SignalCollection(tuple(signals), tuple(unread))


def _fleet_loop_policy_signals() -> SignalCollection:
    """One WARNING when this box's fleet loop declaration is unsatisfiable.

    ``deploy/entrypoint.sh`` resolves a contradictory ``TEATREE_DISABLED_LOOPS``
    correctly (it prunes the unmaskable names and continues rather than crash-looping
    init on the config the box already shipped) and warns on stderr — but that stderr
    scrolls away with the deploy log, so a declaration that masks NOTHING, and that
    silently displaced the built-in default, persists unnoticed across every redeploy.
    Compose passes the env file to every service, so the same declaration the init
    role read is readable here; this turns the transient warning into a durable
    :class:`KnownIssue` row that clears on its own once the repo variable is fixed.

    A sound or partially-pruned declaration emits nothing. An env read cannot fail,
    so this collector has no ``unread`` state of its own.
    """
    import os  # noqa: PLC0415 — deferred: keeps the module cold-import cheap, like the sibling collectors

    from teatree.config.fleet_policy import (  # noqa: PLC0415 — deferred: same cold-import discipline
        FLEET_DISABLED_VARIABLE,
        FLEET_ENABLED_VARIABLE,
        fleet_policy_contradiction,
    )

    reason = fleet_policy_contradiction(
        enabled_raw=os.environ.get(FLEET_ENABLED_VARIABLE),
        disabled_raw=os.environ.get(FLEET_DISABLED_VARIABLE),
    )
    if not reason:
        return SignalCollection()
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="fleet-loop-policy-contradiction",
                severity=KnownIssue.Severity.WARNING,
                kind="config_pair_drift",
                summary=f"fleet loop policy: {reason}",
            ),
        )
    )


def _admission_pressure_signals() -> SignalCollection:
    """At most ONE signal, fingerprinted on the admission scalar's dominant cause (#4508).

    Telemetry volume is not incident volume: a braked factory refuses on every admission
    decision, which is hundreds of identical observations an hour. Routing them through
    the fingerprint the registry already dedupes on collapses them to one row per CAUSE,
    with ``first_seen``/``last_seen`` carrying the duration — and it auto-resolves by
    construction when the pressure falls, so no operator chases a stale entry.

    Nothing is emitted below the shed band: a factory merely under load is not an
    incident, and a chip that lit up at 0.7 would be ignored by the time it mattered.
    """
    from teatree.core.admission_governor import (  # noqa: PLC0415 — deferred: reads the ORM-backed quota cache at call time
        pressure_for,
        read_machine_signal,
        read_quota_signal,
    )
    from teatree.core.admission_pressure import PressureBand  # noqa: PLC0415 — deferred with its reader

    try:
        pressure = pressure_for(quota=read_quota_signal(), machine=read_machine_signal())
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-admission-pressure", "admission-pressure health read failed", exc_info=True)
        return SignalCollection(unread=("_admission_pressure_signals",))
    dominant = pressure.dominant
    if dominant is None or pressure.band in {PressureBand.FULL, PressureBand.DEGRADED}:
        return SignalCollection()
    critical = pressure.band is PressureBand.HALT
    verb = "refusing every admission" if critical else "shedding expensive work"
    return SignalCollection(
        (
            HealthSignal(
                fingerprint=f"admission-pressure:{dominant.name}",
                severity=KnownIssue.Severity.CRITICAL if critical else KnownIssue.Severity.WARNING,
                kind="admission_pressure",
                summary=f"admission pressure {pressure.value:.2f} — {verb}: {dominant.detail}",
            ),
        )
    )


# The deterministic signal collectors, run in order. Each is fail-open on its
# own so one broken read never suppresses the others; adding a new signal family
# (default-branch CI, stale 404 refs, …) is one entry here plus its collector.
_COLLECTORS = (
    _overlay_health_signals,
    _stale_tick_signals,
    _failed_task_signals,
    _harness_provider_consistency_signals,
    _fleet_loop_policy_signals,
    _admission_pressure_signals,
)


def collect_signals() -> SignalCollection:
    """Run every collector, fail-open, and return the live signals plus the unread sources.

    A collector that raises still never crashes the tick — but it names itself in
    ``unread`` instead of passing for a clean read.
    """
    signals: list[HealthSignal] = []
    unread: list[str] = []
    for collector in _COLLECTORS:
        try:
            collected = collector()
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
            unread.append(collector.__name__)
            warn_throttled(
                logger,
                f"health-collector:{collector.__name__}",
                "health collector %s failed — skipped",
                collector.__name__,
                exc_info=True,
            )
            continue
        signals.extend(collected.signals)
        unread.extend(collected.unread)
    return SignalCollection(tuple(signals), tuple(unread))


def _unread_source_signals(unread: tuple[str, ...]) -> list[HealthSignal]:
    """One loud CRITICAL per source that could not be read.

    Without it the chip renders an unreadable factory exactly as it renders a
    healthy one, which is the whole defect: a suspended auto-resolve keeps the
    known issues visible, and this makes the blindness itself visible.
    """
    return [
        HealthSignal(
            fingerprint=f"health-collector-failed:{label}",
            severity=KnownIssue.Severity.CRITICAL,
            kind="health_collector_failed",
            summary=f"health source {label} could not be read — its signals are unknown, auto-resolve suspended",
        )
        for label in unread
    ]


def _status_from_issues(issues: Iterable[KnownIssue]) -> HealthStatus:
    """Map open issues to a verdict via the spec thresholds."""
    critical = 0
    warning = 0
    for issue in issues:
        if issue.severity == KnownIssue.Severity.CRITICAL:
            critical += 1
        else:
            warning += 1
    if critical or warning >= _RED_YELLOW_THRESHOLD:
        return HealthStatus.RED
    if warning:
        return HealthStatus.YELLOW
    return HealthStatus.GREEN


def read_health() -> HealthReport:
    """Return the verdict + open issues from the persisted rows (read-only).

    The statusline chip renders this every tick — it must not write. Fail-open
    to an all-green empty report on any read error so a broken query never
    blanks the statusline or falsely reddens the chip.
    """
    try:
        issues = tuple(KnownIssue.objects.open())
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-read", "open-issue read failed — chip degraded to green", exc_info=True)
        return HealthReport(status=HealthStatus.GREEN, open_issues=())
    return HealthReport(status=_status_from_issues(issues), open_issues=issues)


def reconcile_health() -> HealthReport:
    """Collect live signals, upsert a row per signal, auto-resolve cleared ones.

    The writing entry point: called from the loop tick and from ``health show``.
    On a COMPLETE observation every auto-derived row whose signal is no longer live
    auto-resolves; manual rows are untouched. When a source could not be read the
    auto-resolve is suspended for that tick (its fingerprints are unknown, not
    cleared) and each unread source is recorded as its own CRITICAL, so the tick
    reads RED rather than reporting the unreadable factory as healthy. Returns the
    fresh :class:`HealthReport`. Fail-open — a signal-collection or write error
    degrades to the read-only view so a broken reconcile never crashes the tick.
    """
    try:
        collection = collect_signals()
        signals = [*collection.signals, *_unread_source_signals(collection.unread)]
        for signal in signals:
            KnownIssue.objects.record_signal(signal)
        KnownIssue.objects.reconcile({s.fingerprint for s in signals}, complete=collection.complete)
    except Exception:
        logger.exception("health reconcile failed — returning read-only view")
    return read_health()
