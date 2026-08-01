"""Loop row lifecycle — the audited delete seam (#3842).

Sibling of :mod:`teatree.loops.preset_admin`. A ``Loop`` row had no delete seam at all: the
Django admin's own button was the only surface, so nothing could carry a confirm and
nothing recorded the removal. This is the seam the CLI verb and the admin's refusal both
point at.

Deletion deliberately has NO referrer refusal, unlike a preset. A ``LoopState`` hold, a
preset ``entries`` key and a ``ModeScheduleSlot`` all reference a loop BY NAME and are
documented to fail open — a dangling key is ignored at read time and surfaced by
``t3 doctor`` (:class:`teatree.core.models.loop_preset.Mode`). So the only guard a loop
delete needs is the shipped-definition confirm.
"""

from teatree.core.models import Loop
from teatree.loops.preset_editing import PresetEditError
from teatree.loops.shipped_guard import require_shipped_delete_confirm

__all__ = ["delete_loop"]


def delete_loop(name: str, *, confirm: str = "") -> None:
    """Delete a loop row, requiring the typed phrase when the loop ships by default."""
    if not Loop.objects.filter(name=name).exists():
        msg = f"no loop named {name!r}"
        raise PresetEditError(msg)
    require_shipped_delete_confirm("loop", name, confirm)
    Loop.objects.filter(name=name).delete()
