"""Forge classification from a URL or git remote — one source of truth.

Both the URL-based and repo-origin-based backend resolvers select the forge
through :func:`forge_from_remote`, so a github.com repo always resolves to the
GitHub backend and a gitlab.com / self-hosted GitLab repo to the GitLab backend
regardless of which PATs an overlay happens to carry (#2025).
"""

from typing import Literal

from teatree.utils.git_remote import web_base_from_remote

FORGES = frozenset({"github", "gitlab"})


def normalize_forge(value: str) -> str:
    """*value* as a known forge token (``github`` / ``gitlab``), or ``""``.

    The single normalization boundary for a forge string that arrives as text
    rather than as a URL — the ``--forge`` CLI flag and the persisted
    ``MergeClear.host_kind`` column — so an unknown token can never reach
    :class:`~teatree.utils.pr_ref.PrRef` as a transport.
    """
    candidate = value.strip().lower()
    return candidate if candidate in FORGES else ""


def forge_from_host(host: str) -> Literal["github", "gitlab", ""]:
    """Classify a bare forge host name (``github.com``, ``gitlab.corp.example``).

    The host-only half of :func:`forge_from_remote`, callable on its own by a
    caller that already holds a host rather than a URL — notably the
    forge-host-keyed ``OverlayConfig.owned_repos`` registry, whose keys are bare
    hosts that :func:`web_base_from_remote` cannot parse.
    """
    lowered = host.strip().lower()
    if "github.com" in lowered:
        return "github"
    if "gitlab" in lowered:
        return "gitlab"
    return ""


def forge_from_remote(remote_url: str) -> Literal["github", "gitlab", ""]:
    """Classify a URL or git remote by its host.

    ``"github"`` for a github.com host, ``"gitlab"`` for gitlab.com or a
    self-hosted GitLab host (host substring ``gitlab``), ``""`` for an
    unrecognised / empty host.
    """
    return forge_from_host(web_base_from_remote(remote_url))
