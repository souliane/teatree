"""Per-loop connector preflight — scope the gate to ONE loop's overlay (LOOP-PR-C).

:func:`teatree.core.connector_preflight.run_connector_preflight` is the
fleet-wide gate: it probes every overlay's connectors at once. A per-loop tick
(``t3 loops tick --loop <name>``) must NOT inherit that fleet scope — probing
that whole fleet there means one overlay's connector
outage ``SystemExit``-s an unrelated loop's tick, taking the whole fleet of
per-loop loops down on a single outage.

This narrows the gate to the loop being run: it preflights ONLY the loop's own
overlay (derived from ``Loop.overlay``), and only once the loop is actually
going to run (admitted + due, via the canonical :func:`loop_admits` verdict). A
disabled/cooling loop — and a loop whose overlay can't be resolved to a single
registered overlay — preflights nothing, so the per-loop tick stays isolated
from every overlay it does not depend on. A loop's OWN connector being down
still ``SystemExit``-s that loop's tick (fail loud), unchanged from the fleet
gate.

The unresolvable case is REPORTED rather than silently skipped. ``Loop.overlay``
defaults to ``""`` and the shipped seed table never sets it, so on a multi-overlay
install with no ambient ``T3_OVERLAY_NAME`` this gate — the only thing standing
between a down connector and a tick of silent no-ops — switched itself off for every
loop, with nothing saying so. "I cannot tell which overlay this loop uses" is
configuration drift an operator must see, not a quiet default.
"""

import logging
import os

from django.utils import timezone

from teatree.core.connector_preflight import run_connector_preflight
from teatree.core.overlay_loader import get_all_overlays, resolve_overlay_name
from teatree.loops.enable_verdict import loop_admits
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


def run_loop_connector_preflight(loop_name: str) -> None:
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    row = Loop.objects.filter(name=loop_name).first()
    if row is None:
        return
    if not (loop_admits(loop_name) and row.is_due(timezone.now())):
        return
    overlay_name = _scoped_overlay_name(row.overlay)
    if overlay_name is None:
        warn_throttled(
            logger,
            f"loop-preflight-unscoped:{loop_name}",
            "loop %r has no resolvable overlay (Loop.overlay is %r and no single overlay / T3_OVERLAY_NAME "
            "resolves it) — its connector preflight is SKIPPED, so a down connector will read as a quiet "
            "0-signal tick. Set the loop's overlay to close the gap.",
            loop_name,
            row.overlay,
        )
        return
    run_connector_preflight(overlay_name)


def _scoped_overlay_name(loop_overlay: str) -> str | None:
    """The single registered overlay a loop's connectors belong to, or ``None``.

    A set ``Loop.overlay`` is canonicalized UP to its registered name (an
    unknown/stale value resolves to ``None`` → skip, never the fleet). A blank
    overlay resolves to the one overlay on a single-overlay install (so a
    bare-config install keeps the original fail-loud gate), or to the ambient
    ``T3_OVERLAY_NAME`` on a multi-overlay install. ``None`` (unresolvable) is
    the resilient default: preflight nothing rather than fall back to probing
    every overlay.
    """
    if loop_overlay:
        return resolve_overlay_name(loop_overlay)
    overlays = get_all_overlays()
    if len(overlays) == 1:
        return next(iter(overlays))
    return os.environ.get("T3_OVERLAY_NAME") or None


__all__ = ["run_loop_connector_preflight"]
