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
