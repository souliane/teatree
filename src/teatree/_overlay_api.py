"""Overlay API version pin (re-exported from ``teatree``).

Guards the overlay-facing API: ``OverlayBase`` method signatures, ``Worktree``/
``Ticket`` fields overlays read, the ``teatree.overlays`` entry-point contract,
or the runner protocols an overlay may implement. Overlays assert this at import
and fail loudly on a mismatch, so a genuinely out-of-sync overlay install (a
teatree upgraded independently of the overlay it was built against) is caught at
import rather than misbehaving at runtime.

**Held at "1" until the first stable release — a deliberate policy, not an
oversight.** Pre-1.0, the overlay-facing API is explicitly unstable and every
registered overlay is migrated IN LOCKSTEP with each breaking core change: the
PR-27b (#3067) ``OverlayBase`` 47→11 composed-facet reshape, for example, is a
breaking *surface* change, but the same wave migrated every registered overlay
onto the composed facets, so no overlay ever loads against an incompatible base
(they load and pass conformance against the reshaped base at "1" today). Because
core and overlays move together pre-stable, bumping the counter on each such
change would only manufacture an artificial teatree-vs-overlay mismatch (and the
chicken-and-egg of a lockstep double-bump) with nothing to detect. The pin is
therefore intentionally frozen at "1" through the pre-stable window; it starts
tracking breaking changes as a genuine compatibility counter only from the first
stable release, when overlays may pin an older teatree and the mismatch guard
becomes load-bearing. (An earlier 1→2 bump was reverted in ``2aaff7f25`` for
exactly this reason; see souliane/teatree#3157 AH-8.)
"""

__overlay_api_version__ = "1"
