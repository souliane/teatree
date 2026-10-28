"""Resolve the smallest known repository set for ``workspace ticket``."""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from teatree.config import discover_active_overlay
from teatree.core.overlay_ownership import bare_name_owns, full_slug_owns
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
    at a product repo.
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

    Precedence: explicit ``--repos``, overlay issue hint, dev repo, then
    the single overlay entry that owns the issue's repo slug. An unknown
    issue returns no repos, to be scoped before provisioning.

    #33: a ``--repos`` token may carry a per-repo branch as ``repo:branch``
    (e.g. ``"backend:fix/123,frontend:main"``) so split-branch repos compose
    as siblings in one ticket dir. The branch suffix is stripped here — the
    repo NAME is the part before the first ``:`` — and parsed separately by
    :func:`parse_repo_branch_map`.
    """
    if repos:
        return [_repo_name(r) for r in repos.split(",") if r.strip()]
    hinted = list(dict.fromkeys(name.strip() for name in overlay.metadata.get_issue_repos(issue_url) if name.strip()))
    if hinted:
        return hinted
    dev_repo = resolve_dev_repo(issue_url)
    if dev_repo:
        return [dev_repo]
    slug = issue_url_slug(issue_url)
    if not slug:
        return []
    declared = overlay.get_workspace_repos()
    full = [name for name in declared if full_slug_owns(name, slug)]
    if full:
        return [max(full, key=len)]
    return [name for name in declared if bare_name_owns(name, slug)][:1]


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
