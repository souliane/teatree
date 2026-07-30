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
    its session's, so the clause spans both relations and always admits the
    legacy empty-overlay rows (pre-multi-overlay data). ``prefix`` reaches the
    ``ticket``/``session`` pair from a related model — ``"task__"`` scopes a
    ``TaskAttempt`` by its task's overlay. An empty/``None`` overlay yields a
    bare ``Q()`` that matches everything (``filter(Q())`` == ``all()``).

    Shared by ``TaskQuerySet.for_overlay`` and the dashboard-selector filters
    (``selectors._filters``) so the Task overlay clause can never drift between
    the manager path and the read-model path (F1.6).
    """
    if not overlay:
        return Q()
    ticket = f"{prefix}ticket__overlay"
    session = f"{prefix}session__overlay"
    return Q(**{ticket: overlay}) | Q(**{session: overlay}) | Q(**{ticket: ""}) | Q(**{session: ""})


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
