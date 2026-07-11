"""The single home for forge classification and PR/MR-URL parsing.

Every "is this a GitHub PR or a GitLab MR URL, and what repo/number does it
name" question routes here, built on the canonical :class:`~teatree.utils.pr_ref.PrRef`
parser. The forge vocabulary (``github`` / ``gitlab``) matches the
``host_kind`` string the merge-execution transport already dispatches on.

The ``merge_requests`` / ``/pull/`` path-shape literals and the GitLab-MR URL
regex live here and nowhere else (an AST hook bans them outside this module),
so a new forge-URL consumer parses through these functions instead of
re-deriving the path grammar.
"""

import re
from enum import StrEnum

from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

_GITLAB_MARKER = "/-/merge_requests/"
_GITHUB_MARKERS = ("/pull/", "/pulls/")

_PR_URL_RE = re.compile(r"https?://[^\s|>]+/(?:merge_requests|pull|pulls)/\d+")


class Forge(StrEnum):
    """Which forge a PR/MR URL belongs to. ``UNKNOWN`` for a non-PR/MR URL."""

    GITHUB = "github"
    GITLAB = "gitlab"
    UNKNOWN = "unknown"


def forge_of(url: str) -> Forge:
    """Classify *url* as a GitLab MR, a GitHub PR, or neither.

    Keyed on the path shape, not the hostname: a GitLab MR path contains
    ``/-/merge_requests/`` and a GitHub PR path contains ``/pull(s)/``. Anything
    else (an issue URL, a bare repo URL, blank) is :attr:`Forge.UNKNOWN`.
    """
    if _GITLAB_MARKER in url:
        return Forge.GITLAB
    if any(marker in url for marker in _GITHUB_MARKERS):
        return Forge.GITHUB
    return Forge.UNKNOWN


def is_gitlab_mr_url(url: str) -> bool:
    """True for a GitLab MR URL (``/-/merge_requests/<iid>``)."""
    return forge_of(url) is Forge.GITLAB


def is_github_pr_url(url: str) -> bool:
    """True for a GitHub PR URL (``/pull(s)/<n>``)."""
    return forge_of(url) is Forge.GITHUB


def pr_ref(url: str) -> PrRef | None:
    """Parse *url* into a :class:`PrRef` (slug, pr_id, host_kind), or ``None``."""
    return pr_ref_from_url(url)


def repo_and_iid(url: str) -> tuple[str, int] | None:
    """Return ``(<repo slug>, <number>)`` for a PR/MR URL, or ``None``.

    The (repo, iid) pair every ``glab -R <project> <iid>`` / GitHub-API caller
    needs, parsed through the one :func:`pr_ref_from_url`. The repo slug carries
    nested GitLab subgroups (``team/sub/api``) unchanged.
    """
    ref = pr_ref_from_url(url)
    if ref is None:
        return None
    return ref.slug, ref.pr_id


def find_pr_urls(text: str) -> list[str]:
    """Every PR/MR URL embedded in free *text*, in order of appearance."""
    return _PR_URL_RE.findall(text)


def first_pr_url(text: str) -> str:
    """The first PR/MR URL in *text* (trailing slash and ``#`` fragment stripped), or ``""``."""
    match = _PR_URL_RE.search(text)
    if match is None:
        return ""
    return match.group(0).rstrip("/").split("#")[0]


__all__ = [
    "Forge",
    "PrRef",
    "find_pr_urls",
    "first_pr_url",
    "forge_of",
    "is_github_pr_url",
    "is_gitlab_mr_url",
    "pr_ref",
    "repo_and_iid",
]
