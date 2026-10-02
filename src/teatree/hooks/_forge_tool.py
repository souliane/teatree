"""Which forge CLI a repo slug routes to.

Pure routing, no I/O: the host segment of a slug decides whether ``gh`` or
``glab`` can answer for it. The visibility probe and the foreign-open-MR guard
both need that decision and only one of them needs the privacy question around
it, so it lives here rather than in :mod:`teatree.hooks._repo_visibility` —
which is at its module-health public-function ceiling and cannot take it.

The decision is not cosmetic: a shell gate that hard-codes ONE forge can only
ever ask the forge the branch does not live on, and a guard that cannot ask
reads exactly like a guard that found nothing.
"""

from typing import Final

#: The two forges a slug can route to. ``""`` is "no recognised route".
GITHUB: Final[str] = "github"
GITLAB: Final[str] = "gitlab"

#: Which CLI answers for each forge.
FORGE_TOOL: Final[dict[str, str]] = {GITHUB: "gh", GITLAB: "glab"}

# A slug must have at least ``owner/repo`` (host-prefixed slugs add more).
_MIN_SLUG_PARTS: Final[int] = 2


def host_of_slug(slug: str) -> str:
    """The host segment of *slug* lowercased, or ``""`` for a bare ``owner/repo``."""
    first = slug.split("/", maxsplit=1)[0]
    return first.lower() if "." in first else ""


def forge_and_repo_path(slug: str) -> tuple[str, str]:
    """Return ``(forge, owner/repo)`` for *slug*, or ``("", "")`` when it routes nowhere.

    The forge comes from the slug's host segment — a first ``/``-segment
    containing a dot (``gitlab.com/...`` → :data:`GITLAB`, ``github.com/...`` →
    :data:`GITHUB`). A BARE ``owner/repo`` carries no host and defaults to
    :data:`GITHUB`; callers that know the forge from the publish tool qualify the
    slug up first (``_repo_visibility.forge_qualified_slug``). The returned path
    is the host-stripped ``owner/repo`` each CLI takes as its repo argument.
    """
    parts = slug.split("/")
    if len(parts) < _MIN_SLUG_PARTS:
        return "", ""
    host = parts[0] if "." in parts[0] else ""
    repo_path = "/".join(parts[1:]) if host else slug
    if host.startswith(GITLAB):
        return GITLAB, repo_path
    if host.startswith(GITHUB) or not host:
        return GITHUB, repo_path
    return "", ""
