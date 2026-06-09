"""Resolve an overlay instance from a forge URL.

The URL-context counterpart of the ticket/worktree/repo resolvers in
:mod:`teatree.core.overlay_loader`. Kept in its own module so the loader stays
under the module-health public-function cap; it is re-exported from
``overlay_loader`` for callers that import it alongside the sibling resolvers.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def get_overlay_for_url(url: str) -> "OverlayBase":
    """Resolve the overlay that owns the forge ``url``, raising if it can't.

    The URL-context counterpart of ``get_overlay_for_ticket`` /
    ``get_overlay_for_worktree``, for loop-tick call sites that hold a forge
    issue/PR URL but no ticket/worktree row — the multi-overlay tick process
    registers every installed overlay, so a bare ``get_overlay`` raises
    ``Multiple overlays found`` (souliane/teatree#1814 class). The URL itself
    records which overlay owns it via ``infer_overlay_for_url``'s repo-ownership
    match, so resolution is unambiguous whenever the URL maps to exactly one
    overlay.

    When the URL maps to no overlay (or to more than one — ambiguous ownership),
    resolution falls through to ``get_overlay`` with no name: the ambient
    single-overlay default still resolves, and a genuinely ambiguous
    multi-overlay environment raises the explicit ``Multiple overlays found
    (...)`` error naming the installed overlays — fail loud, never silently pick
    one.
    """
    from teatree.core.overlay_loader import get_overlay, infer_overlay_for_url  # noqa: PLC0415

    return get_overlay(infer_overlay_for_url(url) or None)
