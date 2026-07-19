from django.db.models import Q

from teatree.core.managers import overlay_scope_q


def _overlay_q(overlay: str | None, prefix: str = "") -> Q:
    """Return a Q filter including empty-overlay rows (pre-multi-overlay data).

    Thin delegate to :func:`teatree.core.managers.overlay_scope_q` — the single
    source of truth for the Task overlay clause (F1.6) — so the read-model
    filters can never drift from ``TaskQuerySet.for_overlay``.
    """
    return overlay_scope_q(overlay, prefix=prefix)


def _task_overlay_q(overlay: str | None) -> Q:
    """Return a Q filter for task's ticket/session overlay (from TaskAttempt)."""
    return overlay_scope_q(overlay, prefix="task__")
