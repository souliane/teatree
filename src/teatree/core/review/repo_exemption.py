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

Matching reuses :func:`teatree.hooks._repo_visibility.slug_namespace_matches` —
the host-stripped leading-segment-prefix grammar ``private_repos`` and
:mod:`teatree.core.review.mr_reminder`'s channel routing already share — so a
repo pattern means one thing wherever it is written. A namespace pattern covers
every repo under it; a sibling repo outside the pattern's segments does not match.

Every unresolvable input fails toward NOT exempt: an over-match suppresses a
review request that should have gone out (work stalls with nothing said), while
an under-match posts one the owner would rather be asked for in person (noise a
colleague dismisses once).
"""

from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from teatree.config import get_effective_settings
from teatree.core.overlay_loader import get_overlay
from teatree.hooks._repo_visibility import slug_namespace_matches
from teatree.utils.url_slug import slug_from_issue_or_pr_url


def is_review_exempt(slug: str, *, patterns: tuple[str, ...]) -> bool:
    """Whether repo *slug* is covered by any of *patterns*."""
    if not slug.strip():
        return False
    return any(slug_namespace_matches(pattern, slug) for pattern in patterns)


def review_exempt_patterns(overlay_name: str = "") -> tuple[str, ...]:
    """The declared exempt-repo patterns for *overlay_name* — the setting plus the overlay hook.

    Two independent declarations so an operator can exempt a repo the overlay
    does not know about without editing the overlay, and an overlay can ship its
    own table without every install having to restate it. Order is
    setting-then-overlay and duplicates collapse, so the union is stable.
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
