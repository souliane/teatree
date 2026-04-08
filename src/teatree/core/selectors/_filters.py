from django.db.models import Q


def _overlay_q(overlay: str | None, prefix: str = "") -> Q:
    """Return a Q filter including empty-overlay rows (pre-multi-overlay data)."""
    if not overlay:
        return Q()
    t = f"{prefix}ticket__overlay"
    s = f"{prefix}session__overlay"
    return Q(**{t: overlay}) | Q(**{s: overlay}) | Q(**{t: ""}) | Q(**{s: ""})


def _task_overlay_q(overlay: str | None) -> Q:
    """Return a Q filter for task's ticket/session overlay (from TaskAttempt)."""
    return _overlay_q(overlay, prefix="task__")
