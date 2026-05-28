"""Re-export :class:`MiniLoopMarker` so loops/ stays self-contained.

The Django model lives at :mod:`teatree.core.models.mini_loop_marker`
because the migration ledger lives under ``core/migrations/``. This
module is the import surface mini-loop code uses so the
:mod:`teatree.loops` package reads as a self-contained unit.
"""

from teatree.core.models.mini_loop_marker import MiniLoopMarker, MiniLoopMarkerManager

__all__ = ["MiniLoopMarker", "MiniLoopMarkerManager"]
