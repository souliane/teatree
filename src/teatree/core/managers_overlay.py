"""Overlay-scope Q-builders — the single source of truth for overlay filtering.

Split out of ``managers.py`` so the overlay-query concern lives in its own leaf
module (module-health cap). ``overlay_scope_q`` is the shared Task-overlay clause
used by both the manager path (``TaskQuerySet.for_overlay``) and the read-model
path (``selectors._filters``); ``_for_overlay`` is the plain single-column scope
used by the overlay-carrying QuerySets.
"""

from django.db import models
from django.db.models import Q

__all__ = ["overlay_scope_q"]


def overlay_scope_q(overlay: str | None, *, prefix: str = "") -> Q:
    """The Task-overlay scope clause as a reusable ``Q`` — the single source of truth.

    A ``Task`` has no overlay column of its own: its overlay is its ticket's OR
    its session's, so the clause spans both relations and admits a legacy
    pre-multi-overlay row — one where NEITHER relation names an overlay. Both
    must be blank: a per-relation blank arm exposed one overlay's ticket task
    to every other overlay through its unstamped session, which then claimed it.
    A missing relation counts as blank (SQL ``NULL`` never equals ``''``), so a
    ticket-less or session-less row stays in the legacy set instead of dropping
    out of scope entirely. ``prefix`` reaches the ``ticket``/``session`` pair
    from a related model — ``"task__"`` scopes a ``TaskAttempt`` by its task's
    overlay. An empty/``None`` overlay yields a bare ``Q()`` that matches
    everything (``filter(Q())`` == ``all()``).

    Shared by ``TaskQuerySet.for_overlay`` and the dashboard-selector filters
    (``selectors._filters``) so the Task overlay clause can never drift between
    the manager path and the read-model path (F1.6).
    """
    if not overlay:
        return Q()
    ticket = f"{prefix}ticket__overlay"
    session = f"{prefix}session__overlay"
    ticket_blank = Q(**{f"{prefix}ticket__isnull": True}) | Q(**{ticket: ""})
    session_blank = Q(**{f"{prefix}session__isnull": True}) | Q(**{session: ""})
    return Q(**{ticket: overlay}) | Q(**{session: overlay}) | (ticket_blank & session_blank)


def for_overlay(qs: models.QuerySet, overlay: str | None) -> models.QuerySet:
    """Scope *qs* to *overlay*, including legacy empty-overlay rows.

    A module function rather than a mixin (composition over inheritance): the
    three overlay-scoped QuerySets call it from their own ``for_overlay`` method,
    so there is no mixin diamond and no ``# type: ignore[attr-defined]`` on
    ``self.filter`` / ``self.all``.
    """
    if overlay:
        return qs.filter(Q(overlay=overlay) | Q(overlay=""))
    return qs.all()
