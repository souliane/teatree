"""Overlay API version pin (re-exported from ``teatree``).

Bumped on any breaking change to the overlay-facing API: ``OverlayBase``
method signatures, ``Worktree``/``Ticket`` fields overlays read, the
``teatree.overlays`` entry-point contract, or the runner protocols an
overlay may implement. Overlays assert this at import to fail loudly when
teatree diverges from what they were built against — no silent
misbehavior.

This is a compatibility COUNTER, not a release version: it counts breaking
overlay-API changes, so a pre-1.0 teatree still bumps it on every break. v2
records the PR-27b (#3067) ``OverlayBase`` 47→11 composed-facet reshape — a
breaking change (overlays that overrode the flat methods had to adopt the
composed facets), so a pre-reshape overlay pinned to v1 fails its import
assertion loudly instead of loading against the incompatible facet surface
and misbehaving at runtime.
"""

__overlay_api_version__ = "2"
