"""Overlay API version pin (re-exported from ``teatree``).

Bumped on any breaking change to the overlay-facing API: ``OverlayBase``
method signatures, ``Worktree``/``Ticket`` fields overlays read, the
``teatree.overlays`` entry-point contract, or the runner protocols an
overlay may implement. Overlays assert this at import to fail loudly when
teatree diverges from what they were built against — no silent
misbehavior.
"""

__overlay_api_version__ = "1"
