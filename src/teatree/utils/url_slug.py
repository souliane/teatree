"""Repo-slug extraction from GitHub / GitLab issue & PR/MR web URL paths.

A *slug* is the host-relative project identifier: ``owner/repo`` on
GitHub, ``group/.../repo`` on GitLab. Distinct from the backend regexes
in :mod:`teatree.backends.github` / :mod:`teatree.backends.gitlab`,
which extract the issue *number* for API calls; this module only needs
the project path, so it lives in :mod:`teatree.utils` where both
``core`` and ``backends`` may depend on it.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_GITHUB_RE = re.compile(r"^/(?P<slug>[^/]+/[^/]+)/(?:issues|pull|pulls)/\d+/?$")
_GITLAB_RE = re.compile(r"^/(?P<slug>.+?)/-/(?:issues|work_items|merge_requests)/\d+/?$")

_GITHUB_PR_RE = re.compile(r"^/(?P<slug>[^/]+/[^/]+)/(?:pull|pulls|merge_requests)/(?P<number>\d+)/?$")
_GITLAB_MR_RE = re.compile(r"^/(?P<slug>.+?)/-/merge_requests/(?P<number>\d+)/?$")

# Issue-only (never PR/MR): GitLab issues and merge requests are separate
# numbering sequences within the same project, so a PR/MR path must never
# feed the repo-namespaced key below — issue #5 and merge request !5 could
# otherwise collide on the same key (#2293).
_GITHUB_ISSUE_RE = re.compile(r"^/(?P<slug>[^/]+/[^/]+)/issues/(?P<number>\d+)/?$")
_GITLAB_ISSUE_RE = re.compile(r"^/(?P<slug>.+?)/-/(?:issues|work_items)/(?P<number>\d+)/?$")


def slug_from_issue_or_pr_url(url_path: str) -> str:
    """Return the repo slug for *url_path* (a ``urlparse(...).path``).

    Recognises GitHub ``/<owner>/<repo>/issues|pull/<n>`` and GitLab
    ``/<path>/-/issues|work_items|merge_requests/<iid>``. Returns ``""``
    when *url_path* matches neither shape.
    """
    gitlab = _GITLAB_RE.match(url_path)
    if gitlab is not None:
        return gitlab["slug"]
    github = _GITHUB_RE.match(url_path)
    if github is not None:
        return github["slug"]
    return ""


@dataclass(frozen=True, slots=True)
class PrRef:
    """A parsed PR/MR web URL: repo slug, PR/MR number, and forge transport kind.

    ``host_kind`` is ``"github"`` or ``"gitlab"`` — the same transport switch
    :func:`teatree.core.merge.ci_rollup.fetch_live_head_sha` dispatches on.
    """

    slug: str
    number: int
    host_kind: str


def pr_ref_from_url(url: str) -> PrRef | None:
    """Parse a full PR/MR web URL into a :class:`PrRef`, or ``None`` if unrecognised.

    Recognises GitHub ``https://github.com/<owner>/<repo>/pull/<n>`` and
    GitLab ``https://<host>/<group>/.../<repo>/-/merge_requests/<iid>``. The
    forge is inferred from the GitLab ``/-/merge_requests/`` path shape and,
    failing that, from a ``gitlab`` hostname; otherwise GitHub.
    """
    parsed = urlparse(url)
    path = parsed.path
    host = (parsed.hostname or "").lower()
    gitlab = _GITLAB_MR_RE.match(path)
    if gitlab is not None:
        return PrRef(slug=gitlab["slug"], number=int(gitlab["number"]), host_kind="gitlab")
    github = _GITHUB_PR_RE.match(path)
    if github is not None:
        host_kind = "gitlab" if "gitlab" in host else "github"
        return PrRef(slug=github["slug"], number=int(github["number"]), host_kind=host_kind)
    return None


def repo_namespaced_key_from_path(url_path: str) -> str:
    """Return the collision-free ``<repo-slug>#<issue-number>`` key for *url_path*.

    Recognises GitHub ``/<owner>/<repo>/issues/<n>`` and GitLab
    ``/<path>/-/issues|work_items/<iid>`` — issue references only, never a
    PR/MR path (see the note on :data:`_GITHUB_ISSUE_RE`). Returns ``""``
    when *url_path* matches neither shape, so a bare-number or non-forge
    ``issue_url`` (a reviewer-role ticket keyed by a PR/MR URL, a
    ``dogfood-smoke://`` scheme, ...) is a deliberate no-op rather than a
    guessed key (#2293).
    """
    gitlab = _GITLAB_ISSUE_RE.match(url_path)
    if gitlab is not None:
        return f"{gitlab['slug']}#{gitlab['number']}"
    github = _GITHUB_ISSUE_RE.match(url_path)
    if github is not None:
        return f"{github['slug']}#{github['number']}"
    return ""


def repo_namespaced_key(url: str) -> str:
    """Return :func:`repo_namespaced_key_from_path` for a full issue *url*.

    The canonical, collision-free ticket-context cache key (#2293):
    ``acme-eng/bugs#42`` and ``acme-product#42`` share a bare numeric IID
    but never collide on this key, since the full repo path is part of it.
    """
    return repo_namespaced_key_from_path(urlparse(url).path)
