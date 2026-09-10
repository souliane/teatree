"""Whether an ADMITTED dream consolidation pass has fallen behind (souliane/teatree#4726).

``t3 doctor check``'s ``_check_dream_staleness`` asks the marker alone, which is right for
a human-invoked advisory: the operator is standing there, and a note about a loop they
turned off costs them one glance. The always-on chip is the opposite surface — it pages
nobody's attention on purpose, so a signal it raises has to be one somebody must act on.

``dream`` ships ``default_enabled=false`` and is masked off under the ``low-token`` and
``off`` presets, so the marker read ALONE reddens every fresh box permanently, and turns a
holiday on the ``off`` preset into a CRITICAL after six days. Suppression is therefore what
moving the read onto that surface costs, and the verdict it suppresses on is the SAME one
``t3 dream tick`` gates its own fire on (``commands/dream.py``) — a chip naming a different
set of deliberate arms than the verdict does is what #4196 exists to stop.

Bootstrap is excluded the way
:meth:`~teatree.core.models.dream_run_marker.DreamRunMarkerManager.is_critically_stale`
excludes it rather than the way ``is_stale`` does: with no successful pass there is no
baseline to regress from, and a fresh install must not redden.

Separated from :mod:`teatree.core.factory.operational_health` the way ``reclaim_is_stalled``
and ``stranded_ticket_count`` are — the aggregator owns folding signals into a verdict, the
predicate owns what "fallen behind" means. A read this predicate cannot make RAISES; whether
that degrades to a blank chip or an ``unread`` source is the aggregator's decision, not one
to preempt by returning a confident "nothing wrong".
"""

import datetime as dt
from dataclasses import dataclass

from teatree.core.models.dream_run_marker import DreamRunMarker


@dataclass(frozen=True)
class DreamFallenBehind:
    """A dream pass that HAS succeeded before and has not for at least one window."""

    #: Past ``CRITICAL_STALE_MULTIPLE`` windows rather than merely past the first.
    critical: bool
    last_succeeded_at: dt.datetime


def dream_fallen_behind(now: dt.datetime) -> DreamFallenBehind | None:
    """The reportable stall, or ``None`` for the three states nobody must act on.

    ``None`` covers a deliberate off (the enable verdict refuses ``dream``), bootstrap
    (no marker, or one that has never succeeded), and a pass that ran inside the window.
    """
    from teatree.loops.enable_verdict import loop_admits  # noqa: PLC0415 — deferred: ORM-backed, cycle-safe

    if not loop_admits(DreamRunMarker.NAME, now):
        return None
    marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
    if marker is None or marker.last_succeeded_at is None:
        return None
    if not DreamRunMarker.objects.is_stale(now):
        return None
    return DreamFallenBehind(
        critical=DreamRunMarker.objects.is_critically_stale(now),
        last_succeeded_at=marker.last_succeeded_at,
    )
