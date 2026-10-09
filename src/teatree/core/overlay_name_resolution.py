"""Overlay-name resolution helpers for :mod:`teatree.core.overlay_loader`.

Carved out of ``overlay_loader.py`` to hold that hub under the 500-LOC
module-health cap: the ambient CWD-overlay resolver (the tier that keeps
``get_overlay`` in agreement with ``teatree.config``'s active-overlay pick) and
the inverse name lookup that stamps ``Ticket.overlay`` at the creation seam.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def cwd_overlay_name(overlays: "dict[str, OverlayBase]") -> str | None:
    """The registered overlay owning the current working directory, or ``None``.

    Keeps the ambient tier of :func:`~teatree.core.overlay_loader.get_overlay` in
    agreement with the one ``teatree.config`` already applies. ``get_effective_settings``
    resolves the active overlay as ``T3_OVERLAY_NAME`` → cwd walked up to its ``manage.py``
    (:func:`teatree.config.discover_active_overlay`) → the single installed
    overlay, while :func:`~teatree.core.overlay_loader.get_overlay` stopped one tier short.
    On a multi-overlay install that made ONE process hold TWO active overlays: the
    settings tier layered an overlay's DB rows while every backend, gate and
    skill resolver raised ``Multiple overlays found`` and was swallowed
    into a "nothing configured" degradation by its caller.

    Consulted ONLY where :func:`~teatree.core.overlay_loader.get_overlay` would otherwise
    raise, and only honoured when the discovered name is actually registered — so the named,
    env-pinned and single-overlay paths stay byte-for-byte unchanged, and a cwd
    that belongs to no registered overlay still fails loud.
    """
    from teatree.config import discover_active_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        entry = discover_active_overlay()
    except Exception:
        logger.debug("ambient overlay discovery failed; falling through to the multi-overlay error", exc_info=True)
        return None
    if entry is None or entry.name not in overlays:
        return None
    return entry.name


def overlay_name_of(overlay: "OverlayBase | None") -> str:
    """The registered name for *overlay*, or ``""`` when it resolves to none.

    The inverse of :func:`~teatree.core.overlay_loader.get_overlay`. Callers that
    already hold the overlay an operation runs under — the ``workspace ticket`` command,
    the CWD-based worktree auto-register — use this to STAMP ``Ticket.overlay`` at the
    creation seam, so attribution never depends on
    :func:`~teatree.core.overlay_loader.infer_overlay_for_url` succeeding. Inference is
    blind to a synthetic ``auto:<branch>`` URL and to an issue filed in a tracker repo no
    overlay declares, and a blank ``overlay`` makes
    :func:`~teatree.core.overlay_loader.get_overlay_for_ticket` fall through to the
    ambiguous ``get_overlay(None)`` (souliane/teatree#1814).
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: avoids the loader import cycle

    for name, candidate in get_all_overlays().items():
        if candidate is overlay:
            return name
    return ""


def resolve_overlay_name(name: str) -> str | None:
    """Return the canonical registered overlay name for *name*, or ``None``.

    The single source of truth for "is this overlay name dispatchable, and
    under what canonical name". A name that is already a registered overlay
    returns unchanged. A name that matches nothing — a
    removed overlay, a synthetic scanner tag, a typo — returns ``None`` so
    callers can fail it permanently instead of crashing on every retry
    (souliane/teatree#1959 poison-pill).

    Callers asking only "is this dispatchable?" test ``resolve_overlay_name(x)
    is not None``; an empty/blank ``name`` is the ambient single-overlay default
    and is the caller's responsibility to special-case (it returns ``None``).
    """
    from teatree.core.overlay_loader import OverlayConfigResolver  # noqa: PLC0415 — deferred: avoids the loader cycle

    if not name:
        return None
    known = set(OverlayConfigResolver.all_names())
    return name if name in known else None
