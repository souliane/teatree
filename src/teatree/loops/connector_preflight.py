"""Per-loop connector preflight — scope the gate to ONE loop's overlay (LOOP-PR-C).

:func:`teatree.core.connector_preflight.run_connector_preflight` is the
fleet-wide gate: it probes every overlay's connectors at once. A per-loop tick
(``t3 loops tick --loop <name>``) must NOT inherit that fleet scope — probing
that whole fleet there means one overlay's connector
outage ``SystemExit``-s an unrelated loop's tick, taking the whole fleet of
per-loop loops down on a single outage.

This narrows the gate to the loop being run: it preflights ONLY the loop's own
overlay (derived from ``Loop.overlay``), and only once the loop is actually
going to run (enabled + due, via the canonical :func:`loop_enabled` verdict). A
disabled/cooling loop — and a loop whose overlay can't be resolved to a single
registered overlay — preflights nothing, so the per-loop tick stays isolated
from every overlay it does not depend on. A loop's OWN connector being down
still ``SystemExit``-s that loop's tick (fail loud), unchanged from the fleet
gate.
"""

import logging
import os

from django.utils import timezone

from teatree.core.connector_preflight import run_connector_preflight
from teatree.core.overlay_loader import get_all_overlays, resolve_overlay_name
from teatree.loop.loop_state_db import loop_enabled

logger = logging.getLogger(__name__)


def run_loop_connector_preflight(loop_name: str) -> None:
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    row = Loop.objects.filter(name=loop_name).first()
    if row is None:
        return
    if not (loop_enabled(loop_name) and row.is_due(timezone.now())):
        return
    overlay_name = _scoped_overlay_name(row.overlay)
    if overlay_name is None:
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
