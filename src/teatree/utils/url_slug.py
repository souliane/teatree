"""Repo-slug extraction from GitHub / GitLab issue & PR/MR web URL paths.

A *slug* is the host-relative project identifier: ``owner/repo`` on
GitHub, ``group/.../repo`` on GitLab. Distinct from the backend regexes
in :mod:`teatree.backends.github` / :mod:`teatree.backends.gitlab`,
which extract the issue *number* for API calls; this module only needs
the project path, so it lives in :mod:`teatree.utils` where both
``core`` and ``backends`` may depend on it.
"""

import re
from urllib.parse import urlparse

from teatree.utils.pr_ref import PrRef

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
        return PrRef(slug=gitlab["slug"], pr_id=int(gitlab["number"]), host_kind="gitlab")
    github = _GITHUB_PR_RE.match(path)
    if github is not None:
        host_kind = "gitlab" if "gitlab" in host else "github"
        return PrRef(slug=github["slug"], pr_id=int(github["number"]), host_kind=host_kind)
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
    """Return the collision-free ticket-context cache key for a full issue *url*.

    The canonical, collision-free ticket-context cache key (#2293):
    ``acme-eng/bugs#42`` and ``acme-product#42`` share a bare numeric IID
    but never collide on this key, since the full repo path is part of it.

    A URL **fragment** is appended when present, so the key stays as unique
    as the ``issue_url`` it is derived from (#102). Synthetic loop tickets
    (the directive interpret + implement tickets, the outer-loop experiment
    tickets) all anchor on ONE umbrella issue and disambiguate solely via a
    fragment — ``.../issues/3009#directive=5`` vs ``#directive-impl=5`` vs
    ``#outer-loop-experiment=7``. Without the fragment they would all collapse
    to ``souliane/teatree#3009`` and collide on the ``Ticket`` unique
    constraint; the fragment restores the "distinct ``issue_url`` ⇒ distinct
    key" invariant the constraint depends on. A real ``.../issues/42`` (no
    fragment) is unchanged.
    """
    parsed = urlparse(url)
    base = repo_namespaced_key_from_path(parsed.path)
    if base and parsed.fragment:
        return f"{base}#{parsed.fragment}"
    return base


def project_slug_from_ref(ref: str) -> str:
    """Normalize a CLI-supplied *ref* to the canonical repo-project slug (#2892).

    Accepts either a literal ``owner/repo`` slug or a full issue/PR/MR web
    URL, resolving the URL case through the same :func:`slug_from_issue_or_pr_url`
    parser #2293 introduced — one repo-slug extraction mechanism, reused
    rather than duplicated, for both the issue-scoped
    :func:`repo_namespaced_key` and this project-scoped (repo-only) key.
    Returns ``""`` for an empty *ref* or an unrecognised URL shape.
    """
    if not ref:
        return ""
    parsed = urlparse(ref)
    if parsed.scheme in {"http", "https"}:
        return slug_from_issue_or_pr_url(parsed.path)
    return ref.strip("/")
