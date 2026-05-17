"""Repo-slug extraction from GitHub / GitLab issue & PR/MR web URL paths.

A *slug* is the host-relative project identifier: ``owner/repo`` on
GitHub, ``group/.../repo`` on GitLab. Distinct from the backend regexes
in :mod:`teatree.backends.github` / :mod:`teatree.backends.gitlab`,
which extract the issue *number* for API calls; this module only needs
the project path, so it lives in :mod:`teatree.utils` where both
``core`` and ``backends`` may depend on it.
"""

import re

_GITHUB_RE = re.compile(r"^/(?P<slug>[^/]+/[^/]+)/(?:issues|pull|pulls)/\d+/?$")
_GITLAB_RE = re.compile(r"^/(?P<slug>.+?)/-/(?:issues|work_items|merge_requests)/\d+/?$")


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
