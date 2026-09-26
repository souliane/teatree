"""Which credential a remote's MRs are authored under — keyed on the REPO, not on the caller.

An overlay may declare that one repo is written under a bot credential so the human owner stays
eligible to approve its MRs (:meth:`OverlayConfig.get_gitlab_token_for_remote`). That declaration
used to be read off whichever overlay happened to be AMBIENT, so the same repo answered a
different identity depending on which ``t3 <overlay>`` prefix was typed — and an entrypoint
pinned to an overlay carrying no declaration (a pre-push hook, a fixed CLI prefix) opened every
MR under the owner, the one identity a forge refuses an approval from. Those MRs are structurally
unapprovable, and nothing says so until somebody tries to approve one.

Reading the declaration off EVERY registered overlay makes the authoring identity a property of
the remote, so every entrypoint resolves the same one and the identity stops depending on which
command someone happened to type. Two overlays declaring DIFFERENT credentials for one remote is
a configuration conflict, raised rather than guessed through.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.identity_wiring import (
    AuthoringIdentity,
    authoring_identity_fault,
    classify_authoring_identity,
    unapprovable_author_fault,
)
from teatree.core.overlay import OverlayConfig
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.utils import git

logger = logging.getLogger(__name__)


class AuthenticatedHost(Protocol):
    """The one thing the pre-create gate needs of a code host: who it authenticates as."""

    def current_user(self) -> str: ...  # pragma: no branch


@dataclass(frozen=True, slots=True)
class DeclaredAuthor:
    """The overlay that declares a non-owner author for a remote, and the credential it names."""

    overlay: str
    token: str


class AmbiguousAuthoringCredentialError(RuntimeError):
    """Two registered overlays declare DIFFERENT non-owner credentials for one remote."""


@dataclass(frozen=True, slots=True)
class RemoteAuthoring:
    """What the registered overlays collectively say about one remote's author.

    ``unreachable_in`` is the half an ambient-only read cannot see: an overlay OTHER than the
    caller's declares a non-owner author for this remote and its credential does not resolve
    here, so MRs would be opened by the owner — who the forge then bars from approving them.
    """

    declared: DeclaredAuthor | None
    unreachable_in: tuple[str, ...]


_declared_cache: dict[str, RemoteAuthoring] = {}

_NOTHING_DECLARED = RemoteAuthoring(declared=None, unreachable_in=())


def reset_authoring_credential_cache() -> None:
    """Drop the per-remote declaration memo — call when the overlay set or its secrets change."""
    _declared_cache.clear()


def overlay_authoring_for(remote: str) -> RemoteAuthoring:
    """What every REGISTERED overlay declares about *remote*'s author, memoized per remote."""
    if not remote:
        return _NOTHING_DECLARED
    if remote not in _declared_cache:
        _declared_cache[remote] = _scan_registered_overlays(remote)
    return _declared_cache[remote]


def declared_distinct_author(remote: str) -> DeclaredAuthor | None:
    """The non-owner credential a REGISTERED overlay declares for *remote*, or ``None``.

    Only a credential that RESOLVED to something other than its overlay's own answers: an
    unreachable one is missing evidence rather than a declaration to act on, and is reported
    separately as :attr:`RemoteAuthoring.unreachable_in`.
    """
    return overlay_authoring_for(remote).declared


def _scan_registered_overlays(remote: str) -> RemoteAuthoring:
    by_token: dict[str, list[str]] = {}
    unreachable: list[str] = []
    for name, overlay in get_all_overlays().items():
        config = overlay.config
        identity = config.authoring_identity_on(remote)
        if identity is AuthoringIdentity.UNRESOLVABLE:
            unreachable.append(name)
        elif identity is AuthoringIdentity.DISTINCT:
            by_token.setdefault(config.get_gitlab_token_for_remote(remote), []).append(name)
    if len(by_token) > 1:
        conflicting = ", ".join(sorted(name for names in by_token.values() for name in names))
        msg = (
            f"overlays {conflicting} declare DIFFERENT non-owner credentials for {remote} — "
            f"refusing to guess which identity its MRs are authored under; scope the remote to "
            f"one credential in the overlays' configs"
        )
        raise AmbiguousAuthoringCredentialError(msg)
    declared = None
    if by_token:
        token, names = next(iter(by_token.items()))
        declared = DeclaredAuthor(overlay=min(names), token=token)
    return RemoteAuthoring(declared=declared, unreachable_in=tuple(sorted(unreachable)))


def _declared_or_none(remote: str, *, context: str) -> DeclaredAuthor | None:
    """*remote*'s declared author, degrading an unreadable overlay registry to ``None``.

    A venue with no Django app registry cannot enumerate overlays; that is a known state, not a
    conflict, so it falls back to the ambient answer. A genuine conflict still raises.
    """
    try:
        return declared_distinct_author(remote)
    except AmbiguousAuthoringCredentialError:
        raise
    except Exception:  # noqa: BLE001 — an unreadable overlay registry degrades; it never blocks a push.
        logger.warning("could not enumerate overlays to resolve the author of %s — %s", remote, context)
        return None


def gitlab_token_for_remote(config: OverlayConfig, remote: str) -> str:
    """The GitLab credential *remote* must be written under, whichever overlay is asking."""
    declared = _declared_or_none(remote, context="using the ambient overlay's own credential")
    return declared.token if declared is not None else config.get_gitlab_token_for_remote(remote)


def authoring_identity_for_remote(remote: str, *, fallback: OverlayConfig) -> AuthoringIdentity:
    """Whose credential *remote*'s MRs are written under, asked of the repo rather than one overlay.

    The repo-keyed sibling of :meth:`OverlayConfig.authoring_identity_on`, which answers only for
    the overlay it is called on and so reports OWNER for a repo a DIFFERENT overlay declares —
    including when that overlay's declared credential is unreachable from this venue, the one
    state that silently turns every MR on the repo into one nobody can approve.
    """
    authoring = overlay_authoring_for(remote)
    if authoring.declared is not None:
        return AuthoringIdentity.DISTINCT
    if authoring.unreachable_in:
        return AuthoringIdentity.UNRESOLVABLE
    return classify_authoring_identity(
        owner_token=fallback.get_gitlab_token(), scoped_token=fallback.get_gitlab_token_for_remote(remote)
    )


def approver_identities() -> frozenset[str]:
    """The identities this deployment admits as approvers of its own MRs."""
    from teatree.config import (  # noqa: PLC0415 — deferred: config read at call time
        effective_independent_reviewer_identities,
        get_effective_settings,
    )

    return effective_independent_reviewer_identities(get_effective_settings())


def unapprovable_author_refusal(host: AuthenticatedHost, remote: str) -> str:
    """The named reason an MR on *remote* must NOT be opened under the credential in hand, or ``""``.

    Scoped to remotes that DECLARE a non-owner author: an ordinary repo the owner authors himself
    is never gated and its identity is never even read. On a declared one the authenticated user
    is read back and compared to the approver allowlist, because an MR its own author cannot
    approve is worth refusing before it exists rather than discovering at approval time.
    """
    if _declared_or_none(remote, context="leaving the create ungated") is None:
        return ""
    fault = unapprovable_author_fault(
        remote=remote, authenticated=_read_back_identity(host), approvers=approver_identities()
    )
    return f"{fault.summary} Fix: {fault.remedy}" if fault is not None else ""


def unresolvable_author_refusal(repo_path: str, *, overlay_config: OverlayConfig) -> str:
    """Why no code host could be built, when the cause is a DECLARED author that will not resolve.

    ``""`` when that is not the cause, so the caller keeps its generic message.

    A backend correctly declines to build a host on an empty scoped token rather than falling
    back to the overlay-wide one — but "no code host configured" names neither the cause nor the
    fix, so it reads as "no forge here" and the next move is a raw ``glab`` holding the OWNER's
    token. That MR is then unapprovable by its own author and has to be re-created, so the wrong
    path has to be loud.

    Never raises: one caller is the git pre-push hook, where an exception aborts the push itself,
    and a probe that cannot answer must leave the generic message in place.
    """
    try:
        remote = git.remote_url(repo=repo_path)
        if not remote:
            return ""
        fault = authoring_identity_fault(
            remote=remote, identity=authoring_identity_for_remote(remote, fallback=overlay_config)
        )
    except Exception:  # noqa: BLE001 — a pre-push hook must never raise; degrade to the generic message.
        logger.warning("could not resolve the declared authoring identity for %s", repo_path)
        return ""
    return f"{fault.summary} Fix: {fault.remedy}" if fault is not None else ""


def authorized_pr_host(host: CodeHostBackend | None, repo_path: str) -> CodeHostBackend | str:
    """*host*, or the named reason a PR on *repo_path* may not be opened with it.

    A ``str`` return is a refusal, surfaced BEFORE the create: a declared author this venue
    cannot act as, no configured credentials at all, or an author the forge would then bar from
    approving its own MR — after the create the only remedy for the latter is to close the MR
    and open another.
    """
    if host is None:
        return _no_host_refusal(repo_path)
    return unapprovable_author_refusal(host, git.remote_url(repo=repo_path)) or host


def _no_host_refusal(repo_path: str) -> str:
    """The named unresolvable-author cause behind a missing host, or the generic message."""
    try:
        overlay_config = get_overlay().config
    except Exception:  # noqa: BLE001 — an unresolvable overlay leaves the generic message in place.
        return "no code host configured"
    return unresolvable_author_refusal(repo_path, overlay_config=overlay_config) or "no code host configured"


def _read_back_identity(host: AuthenticatedHost) -> str:
    """The forge username the credential in hand authenticates as, ``""`` when unreadable."""
    try:
        return str(host.current_user())
    except Exception:  # noqa: BLE001 — an unreadable identity is reported as a fault, never raised at the caller.
        logger.warning("could not read back the authenticated forge identity")
        return ""
