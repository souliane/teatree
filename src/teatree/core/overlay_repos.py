"""The full set of forge repos an overlay works in.

Issue intake uses this set when its configured repo list is empty; external
outcomes and other overlay-wide consumers always use the full set.
"""

from typing import TYPE_CHECKING

from teatree.core.merge import normalize_repo_slug

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def owned_repo_slugs(overlay: "OverlayBase | None") -> tuple[str, ...]:
    """The ``owner/name`` slugs of the repos this overlay works in.

    Unions the overlay's followup repos (where the factory files and picks up issues)
    with its declared merge-candidate working repos (e.g. an ``e2e`` companion), each
    normalized up to ``owner/repo``. An overlay with no repo declarations (or none at
    all) yields ``()`` — with no configured intake list, the scanner then keeps
    the pre-scope global author search.
    """
    if overlay is None:
        return ()
    slugs: list[str] = []
    for value in (*overlay.review.merge_candidate_repo_slugs(), *overlay.metadata.get_followup_repos()):
        slug = normalize_repo_slug(value)
        if slug and slug not in slugs:
            slugs.append(slug)
    return tuple(slugs)
