"""Self-repo detection for ``workspace ticket`` provisioning (#727).

``t3 <overlay> workspace ticket <issue_url>`` defaults to the overlay's
*product* repo set (``overlay.get_workspace_repos()``). When the issue
actually lives on the **overlay package repo** or on **teatree core**,
that default provisions the wrong repos and forces a manual
``git worktree add`` workaround.

This module resolves the issue URL's ``owner/repo`` slug and matches it
against the ``origin`` slug of teatree core (the running clone) and of
the active overlay's package repo. A match means "single-repo,
dev-tooling mode" — provision only that repo. No match means "use the
product repo set" (the unchanged default).
"""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from teatree.config import discover_active_overlay
from teatree.project import find_project_root
from teatree.utils import git
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def issue_url_slug(issue_url: str) -> str:
    """Return the ``owner/repo`` (or ``group/sub/repo``) slug for *issue_url*.

    Recognises GitHub ``/<owner>/<repo>/issues|pull/<n>`` and GitLab
    ``/<path>/-/issues|work_items/<iid>`` web URLs. Returns ``""`` for a
    blank or unrecognised URL.
    """
    if not issue_url:
        return ""
    path = urlparse(issue_url).path
    return slug_from_issue_or_pr_url(path)


def resolve_dev_repo(issue_url: str) -> str | None:
    """Return the single self-repo to provision for *issue_url*, or ``None``.

    Returns the matching repo slug when *issue_url* belongs to teatree
    core (the running clone's ``origin``) or to the active overlay's
    package repo. Returns ``None`` when the URL is unrecognised or points
    at a product repo — the caller then falls back to the overlay's
    product repo set.
    """
    target = issue_url_slug(issue_url)
    if not target:
        return None

    project_root = find_project_root()
    if project_root is not None and git.remote_slug(repo=str(project_root)) == target:
        return target

    entry = discover_active_overlay()
    overlay_path = getattr(entry, "project_path", None) if entry is not None else None
    if overlay_path is not None and git.remote_slug(repo=str(overlay_path)) == target:
        return target

    return None


def resolve_repo_names(overlay: "OverlayBase", issue_url: str, repos: str) -> list[str]:
    """Resolve the repo set ``workspace ticket`` should provision.

    Precedence (#727): an explicit ``--repos`` override always wins.
    Otherwise, when the issue lives on teatree core or the overlay's own
    package repo, provision only that single repo (dev-tooling mode) via
    :func:`resolve_dev_repo`. Otherwise the overlay's product repo set
    (``overlay.get_workspace_repos()``).

    #33: a ``--repos`` token may carry a per-repo branch as ``repo:branch``
    (e.g. ``"backend:fix/123,frontend:main"``) so split-branch repos compose
    as siblings in one ticket dir. The branch suffix is stripped here — the
    repo NAME is the part before the first ``:`` — and parsed separately by
    :func:`parse_repo_branch_map`.
    """
    if repos:
        return [_repo_name(r) for r in repos.split(",") if r.strip()]
    dev_repo = resolve_dev_repo(issue_url)
    if dev_repo:
        return [dev_repo]
    return overlay.get_workspace_repos()


def _repo_name(token: str) -> str:
    """The repo name from a ``--repos`` token, dropping any ``:branch`` suffix."""
    return token.strip().split(":", 1)[0].strip()


def parse_repo_branch_map(repos: str) -> dict[str, str]:
    """Per-repo branch overrides parsed from the ``--repos`` string (#33).

    A token of the form ``repo:branch`` maps that repo to its own branch;
    a bare ``repo`` token contributes nothing (the repo falls back to the
    ticket's shared ``extra['branch']`` in the provisioner). A branch value
    may itself contain ``:`` (rare), so only the FIRST ``:`` splits repo from
    branch. Returns an empty dict when no token carries a branch.
    """
    pairs: dict[str, str] = {}
    for token in repos.split(","):
        repo, sep, branch = token.strip().partition(":")
        if sep and repo.strip() and branch.strip():
            pairs[repo.strip()] = branch.strip()
    return pairs
