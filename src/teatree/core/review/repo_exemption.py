"""Which repos' merge requests never get a review request — a DECLARED axis.

Exemption is declared, never derived from repo OWNERSHIP
(:class:`teatree.core.review.mr_triage.RepoOwner`). Ownership answers the PATIENT
owner for every repo it does not recognise — the right fail-safe for nag patience
and the wrong one here: reading exemption off it would exempt every repo teatree
has never heard of, and a review request that silently never goes out is the one
failure nobody sees. Both sources below are explicit declarations: the
``review_exempt_repos`` setting and the overlay's
:meth:`~teatree.core.overlay.OverlayReview.review_exempt_repo_slugs` hook. Both
default empty, so the refusal ships inert.

The overlay hook is the DERIVED layer — the repo table an overlay ships — and the
setting is the PIN layer, the operator's own opinion. A pin wins in BOTH
directions: a plain entry ADDS an exemption the overlay never declared, and a
``!``-prefixed one SUBTRACTS one the overlay did. Union alone could only ever
widen, so an overlay-declared exemption outlived every operator opinion and a
changed policy needed a code change and a merge — an escape hatch that did not
escape.

Matching reuses :func:`teatree.hooks._repo_visibility.slug_namespace_matches` —
the host-stripped leading-segment-prefix grammar ``private_repos`` and
:mod:`teatree.core.review.mr_reminder`'s channel routing already share — so a
repo pattern means one thing wherever it is written, negation included. A
namespace pattern covers every repo under it; a sibling repo outside the
pattern's segments does not match.

Both layers feed ONE flat list and the MOST SPECIFIC match decides, specificity
being :func:`~teatree.hooks._repo_visibility.slug_segment_depth` — the same
host-stripped segment axis the match itself runs on. So ``!org/one-repo`` carves a
single repo out of an ``org`` namespace exemption, and the source that wrote a
pattern carries no weight of its own: a pin beats the overlay by being written at
its target's own depth, never by being read second.

Every unresolvable input fails toward NOT exempt: an over-match suppresses a
review request that should have gone out (work stalls with nothing said), while
an under-match posts one the owner would rather be asked for in person (noise a
colleague dismisses once). A TIE at equal depth resolves the same way, so a
contradiction is never read as an exemption.
"""

from typing import Final
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from teatree.config import get_effective_settings
from teatree.core.overlay_loader import get_overlay
from teatree.hooks._repo_visibility import slug_namespace_matches, slug_segment_depth
from teatree.utils.url_slug import slug_from_issue_or_pr_url

_NEGATION_PREFIX: Final[str] = "!"


def is_review_exempt(slug: str, *, patterns: tuple[str, ...]) -> bool:
    """Whether repo *slug* is exempt under *patterns* — deepest match wins, ties do not exempt."""
    if not slug.strip():
        return False
    exempt = False
    winning_depth = -1
    for pattern in patterns:
        negated = pattern.startswith(_NEGATION_PREFIX)
        key = pattern.removeprefix(_NEGATION_PREFIX) if negated else pattern
        if not slug_namespace_matches(key, slug):
            continue
        depth = slug_segment_depth(key)
        if depth > winning_depth or (depth == winning_depth and negated):
            exempt, winning_depth = not negated, depth
    return exempt


def review_exempt_patterns(overlay_name: str = "") -> tuple[str, ...]:
    """The declared exempt-repo patterns for *overlay_name* — the setting plus the overlay hook.

    Two independent declarations so an operator can exempt a repo the overlay
    does not know about without editing the overlay, and an overlay can ship its
    own table without every install having to restate it. Duplicates collapse and
    :func:`is_review_exempt` resolves by depth, so the concatenation order carries
    no meaning — a ``!`` entry subtracts wherever in the list it was written.
    """
    configured = get_effective_settings(overlay_name or None).review_exempt_repos
    declared = (*configured, *_overlay_declared_patterns(overlay_name))
    return tuple(dict.fromkeys(pattern.strip() for pattern in declared if pattern.strip()))


def mr_url_is_review_exempt(mr_url: str, *, overlay_name: str = "") -> bool:
    """Whether *mr_url*'s repo is review-exempt — the single call a command makes.

    An unparsable URL yields no slug and so is never exempt, keeping the refusal
    on the side that still posts.
    """
    slug = slug_from_issue_or_pr_url(urlparse(mr_url).path)
    return is_review_exempt(slug, patterns=review_exempt_patterns(overlay_name))


def _overlay_declared_patterns(overlay_name: str) -> tuple[str, ...]:
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return ()
    return tuple(overlay.review.review_exempt_repo_slugs())
