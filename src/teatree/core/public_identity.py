"""Scoped per-repo git identity for public ``souliane/*`` repos (#762).

Single source of truth: commits on PUBLIC ``souliane/*`` repos use a
GitHub ``users.noreply.github.com`` author identity rather than the
machine's inherited git identity. Strictly scoped by remote host AND
visibility — non-github (e.g. GitLab) and private remotes are excluded
so their own configured identity is left as-is. Reused by the worktree
provisioner and the fast-push commit-identity leak gate
(:meth:`teatree.core.push.fast_push.LeakGateScan._author_identity`).

The gate is HOST-AWARE (#2655): callers pass the full remote URL (host
intact), never the host-stripped slug — a bare ``owner/repo`` from a
GitLab remote would otherwise be resolved against github.com and a
GitLab clone could be stamped with the github identity.
"""

import re
from typing import NotRequired, TypedDict

from teatree.core.forge_pr_probe import forge_url_cli_env
from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail


class MergeResult(TypedDict):
    merged: bool
    pr: int
    slug: str
    auto: bool
    # Populated only when the deprecated direct-merge path refuses and
    # redirects to the sanctioned keystone transition (BLUEPRINT §17.4).
    error: NotRequired[str]


class StampResult(TypedDict, total=False):
    stamped: bool
    repo: str
    slug: str
    reason: str


NOREPLY_RE = re.compile(r"^([0-9]+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com$")

# The canonical author identity for public commits — this account's
# GitHub ``users.noreply.github.com`` address (numeric prefix = the
# GitHub user id). Applied to every PUBLIC GitHub repo (#785); the
# slug must have exactly owner/repo parts before a visibility check.
_OWNER_REPO_PARTS = 2

_CANONICAL_NAME = "souliane"
_CANONICAL_EMAIL = "21343492+souliane@users.noreply.github.com"


def is_noreply_email(email: str) -> bool:
    return bool(NOREPLY_RE.match(email.strip())) if email else False


def _slug_from(remote: str) -> str:
    cleaned = remote.strip().rstrip("/").removesuffix(".git")
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
        cleaned = cleaned.split("/", 1)[1] if "/" in cleaned else cleaned
    elif "@" in cleaned and ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    return cleaned


def _host_from(remote: str) -> str:
    """Extract the bare host of a git remote URL (no user/port).

    Handles the three shapes git remotes take. A
    ``scheme://[user@]host[:port]/owner/repo`` URL and a scp-like
    ``user@host:owner/repo`` URL both yield ``host``; a bare
    ``owner/repo`` slug (no host) yields ``""``. A ``user@`` prefix and a
    ``:port`` suffix are dropped so only the hostname remains.
    """
    cleaned = remote.strip()
    if "://" in cleaned:
        authority = cleaned.split("://", 1)[1].split("/", 1)[0]
    elif "@" in cleaned and ":" in cleaned:
        authority = cleaned.split("@", 1)[1].split(":", 1)[0]
    else:
        return ""
    authority = authority.split("@")[-1]  # drop any user@ that slipped through
    return authority.split(":", 1)[0]  # drop :port


def is_github_host(remote: str) -> bool:
    """True iff ``remote``'s host is github.com (incl. an ssh-alias host).

    A GitHub ssh alias such as ``git@github.com-work:owner/repo``
    (configured in ``~/.ssh/config`` to disambiguate accounts) resolves
    to github.com, so a ``github.com-<alias>`` host counts as github too.
    A non-github host (``gitlab.com``, ``bitbucket.org``, a self-hosted
    GitLab) is NOT github — its bare ``owner/repo`` slug must never be
    resolved against github.com (the host-blind-slug footgun, #2655).
    """
    host = _host_from(remote)
    return host == "github.com" or host.startswith("github.com-")


def is_public_github_remote(remote: str) -> bool:
    """True iff ``remote``'s GitHub repo is PUBLIC, as ``gh repo view`` reports it (#785).

    A malformed slug, a non-github host (never queried: the parser strips the host, so a GitLab remote would
    resolve its bare ``owner/repo`` against github.com, #2655) or a ``gh`` that exits non-zero returns ``False``,
    leaving the inherited identity in place; a missing ``gh`` binary raises. The pre-push gate
    (``refuse-public-push-with-leak.sh``) fails CLOSED on a remote whose visibility it cannot confirm.
    Callers pass the full remote URL, never the host-stripped slug.
    """
    if not remote:
        return False
    if not is_github_host(remote):
        return False
    slug = _slug_from(remote)
    parts = slug.split("/")
    if len(parts) != _OWNER_REPO_PARTS or not parts[0] or not parts[1]:
        return False
    env = forge_url_cli_env(remote)
    if env is None:
        return False
    try:
        result = run_allowed_to_fail(
            ["gh", "repo", "view", slug, "--json", "visibility", "--jq", ".visibility"],
            expected_codes=(0,),
            env=env,
        )
    except CommandFailedError:
        return False
    return result.stdout.strip().upper() == "PUBLIC"


def canonical_noreply_identity() -> tuple[str, str]:
    return _CANONICAL_NAME, _CANONICAL_EMAIL


def set_local_noreply_identity(repo_path: str) -> None:
    """Set the canonical noreply identity in the repo's clone-local git config.

    Writes ``user.name``/``user.email`` via ``git config --local`` —
    which, in a git worktree, writes the shared clone ``.git/config``
    (there is no ``extensions.worktreeConfig`` here, so this is
    clone-local, not per-worktree-isolated). Global/XDG config is never
    touched, so every commit path uses the configured noreply identity
    instead of the inherited one. Caller guarantees ``repo_path`` is a
    public souliane/* clone or worktree.
    """
    name, email = canonical_noreply_identity()
    git.run(repo=repo_path, args=["config", "--local", "user.name", name])
    git.run(repo=repo_path, args=["config", "--local", "user.email", email])
