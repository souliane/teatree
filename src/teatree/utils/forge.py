"""Forge classification from a URL or git remote — one source of truth.

Both the URL-based and repo-origin-based backend resolvers select the forge
through :func:`forge_from_remote`, so a github.com repo always resolves to the
GitHub backend and a gitlab.com / self-hosted GitLab repo to the GitLab backend
regardless of which PATs an overlay happens to carry (#2025).
"""

from typing import Literal

from teatree.utils.git_remote import web_base_from_remote


def forge_from_remote(remote_url: str) -> Literal["github", "gitlab", ""]:
    """Classify a URL or git remote by its host.

    ``"github"`` for a github.com host, ``"gitlab"`` for gitlab.com or a
    self-hosted GitLab host (host substring ``gitlab``), ``""`` for an
    unrecognised / empty host.
    """
    host = web_base_from_remote(remote_url)
    if "github.com" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return ""
