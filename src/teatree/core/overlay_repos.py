"""The one answer to "which forge repos does this overlay work in?".

Both the intake scanner (which issues to claim) and the external-outcome measure
(which repos' merges count as output) need the same slug set, and a second
derivation would let the two disagree about what the factory's own repos are.
"""

from typing import TYPE_CHECKING

from teatree.core.merge import normalize_repo_slug

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def owned_repo_slugs(overlay: "OverlayBase | None") -> tuple[str, ...]:
    """The ``owner/name`` slugs of the repos this overlay works in — the intake scope.

    Unions the overlay's followup repos (where the factory files and picks up issues)
    with its declared merge-candidate working repos (e.g. an ``e2e`` companion), each
    normalized up to ``owner/repo``. An overlay with no repo declarations (or none at
    all) yields ``()`` — the scanner then keeps the pre-scope global author search.
    """
    if overlay is None:
        return ()
    slugs: list[str] = []
    for value in (*overlay.review.merge_candidate_repo_slugs(), *overlay.metadata.get_followup_repos()):
        slug = normalize_repo_slug(value)
        if slug and slug not in slugs:
            slugs.append(slug)
    return tuple(slugs)
