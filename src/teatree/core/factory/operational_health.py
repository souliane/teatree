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
    ``health show``.
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

from django.utils import timezone

from teatree.core.loop_lease_manager import T3_MASTER_SLOT, is_per_loop_owner_slot, is_per_loop_tick_mutex
from teatree.core.models.known_issue import KnownIssue
from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.throttled_log import warn_throttled

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


def _overlay_health_signals() -> list[HealthSignal]:
    """Fold every registered overlay's ``get_health_signals()`` into one list.

    Each overlay is queried independently and fail-open — one overlay raising
    never suppresses another's signals, so a broken overlay degrades to
    "declares nothing" rather than blanking the whole health surface.
    """
    signals: list[HealthSignal] = []
    for name, overlay in get_all_overlays().items():
        try:
            signals.extend(overlay.get_health_signals())
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
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
    return signals


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


def _stale_tick_signals() -> list[HealthSignal]:
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

    Fail-open to ``[]``.
    """
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred so the app registry is only touched at read time

        now = timezone.now()
        lease_model = apps.get_model("core", "LoopLease")
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
        return []
    return [
        HealthSignal(
            fingerprint=f"stale-tick:{row.name}",
            severity=KnownIssue.Severity.WARNING,
            kind="stale_tick",
            summary=f"loop {row.name} has not ticked in over {_TICK_OVERRUN_MULTIPLE}x its cadence",
        )
        for row in stale
    ]


def _failed_task_signals() -> list[HealthSignal]:
    """One warning summarising recently-failed tasks (spec: failed answering tasks).

    Collapses every :class:`~teatree.core.models.task.Task` that FAILED inside
    :data:`_FAILED_TASK_WINDOW` into a single count so N failures are one chip
    line, not N. Fail-open to ``[]``.
    """
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred so the app registry is only touched at read time

        task_model = apps.get_model("core", "Task")
        cutoff = timezone.now() - _FAILED_TASK_WINDOW
        count = task_model.objects.filter(status="failed", created_at__gte=cutoff).count()
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-failed-task", "failed-task health read failed — skipped", exc_info=True)
        return []
    if count <= 0:
        return []
    noun = "task" if count == 1 else "tasks"
    return [
        HealthSignal(
            fingerprint="failed-tasks",
            severity=KnownIssue.Severity.WARNING,
            kind="failed_tasks",
            summary=f"{count} {noun} failed in the last {int(_FAILED_TASK_WINDOW.total_seconds() // 3600)}h",
        ),
    ]


# The deterministic signal collectors, run in order. Each is fail-open on its
# own so one broken read never suppresses the others; adding a new signal family
# (default-branch CI, stale 404 refs, …) is one entry here plus its collector.
_COLLECTORS = (_overlay_health_signals, _stale_tick_signals, _failed_task_signals)


def collect_signals() -> list[HealthSignal]:
    """Run every collector, fail-open, and return the union of live signals."""
    signals: list[HealthSignal] = []
    for collector in _COLLECTORS:
        try:
            signals.extend(collector())
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
            warn_throttled(
                logger,
                f"health-collector:{collector.__name__}",
                "health collector %s failed — skipped",
                collector.__name__,
                exc_info=True,
            )
    return signals


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
    Every auto-derived row whose signal is no longer live auto-resolves; manual
    rows are untouched. Returns the fresh :class:`HealthReport`. Fail-open — a
    signal-collection or write error degrades to the read-only view so a broken
    reconcile never crashes the tick.
    """
    try:
        signals = collect_signals()
        for signal in signals:
            KnownIssue.objects.record_signal(signal)
        KnownIssue.objects.reconcile({s.fingerprint for s in signals})
    except Exception:
        logger.exception("health reconcile failed — returning read-only view")
    return read_health()
