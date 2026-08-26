"""Resolving, redacting and diagnosing the forge-write credential (souliane/teatree#3927).

Split out of :mod:`teatree.core.forge_push` so that module holds only the push
and its verdicts. Nothing here imports back from it, so the dependency runs one
way: push -> credential.
"""

import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

REDACTION = "<redacted>"

#: Userinfo prefixes that identify a forge token embedded in a remote URL. A bare
#: ``https://<user>@host`` is a legitimate (non-secret) remote and is left alone;
#: only a password component or one of these prefixes marks a URL as secret-bearing.
_FORGE_TOKEN_PREFIXES: tuple[str, ...] = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "glpat-",
    "glptt-",
)

#: git's stderr when it wanted a credential and could not get one — the class of
#: failure whose readable cause is the credential chain, not the push itself.
CREDENTIAL_FAILURE_MARKERS: tuple[str, ...] = (
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "authentication failed",
)


class CredentialSource(StrEnum):
    """Where the forge-write credential came from, in resolution order."""

    GH_TOKEN = "GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    TEATREE_GH_TOKEN = "TEATREE_GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    OVERLAY_PASS_STORE = "overlay pass store"  # noqa: S105 — a source label, not a credential
    AMBIENT = "ambient git credential helper"


@dataclass(frozen=True)
class ForgeCredential:
    """A resolved forge-write token plus the source it came from."""

    token: str
    source: CredentialSource


def scrub_token(text: str, token: str) -> str:
    """Replace every occurrence of *token* in *text* with :data:`REDACTION`."""
    return text.replace(token, REDACTION) if token else text


def _overlay_github_token() -> str:
    """The active overlay's ``pass``-store GitHub token; ``""`` when unavailable.

    Best-effort by design: ``t3 push`` must work in a bare clone with no Django
    settings, no DB, and no registered overlay, so an unresolvable overlay
    degrades to the ambient credential helper instead of raising.
    """
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: Django-dependent

        return get_overlay().config.get_github_token()
    except Exception:  # noqa: BLE001 — any overlay/Django/pass failure is "no token from here"
        return ""


def resolve_forge_credential() -> ForgeCredential:
    """Resolve the forge-write token: env first, then the overlay ``pass`` store.

    ``GH_TOKEN`` is what the deploy entrypoint exports for the role process;
    ``TEATREE_GH_TOKEN`` is the compose ``env_file`` name a ``docker exec`` shell
    inherits instead. The overlay getter is the same one
    ``loop/scanner_factories`` reads, so every forge write on the box resolves
    through one chain.
    """
    for source, name in (
        (CredentialSource.GH_TOKEN, "GH_TOKEN"),
        (CredentialSource.TEATREE_GH_TOKEN, "TEATREE_GH_TOKEN"),
    ):
        token = os.environ.get(name, "")
        if token:
            return ForgeCredential(token=token, source=source)
    token = _overlay_github_token()
    if token:
        return ForgeCredential(token=token, source=CredentialSource.OVERLAY_PASS_STORE)
    return ForgeCredential(token="", source=CredentialSource.AMBIENT)


def remote_url_embeds_credential(url: str) -> bool:
    """Whether *url* carries a secret in its userinfo (a password or a forge token).

    An scp-style SSH remote (``git@host:owner/repo.git``) has no userinfo to
    ``urlsplit``, so it is never flagged; a URL malformed enough to make
    ``urlsplit`` raise carries no parseable userinfo either.
    """
    try:
        parts = urlsplit(url)
        password = parts.password
        username = parts.username or ""
    except ValueError:
        return False
    if password is not None:
        return True
    return username.startswith(_FORGE_TOKEN_PREFIXES)


def credential_failure_hint(git_stderr: str, credential: ForgeCredential) -> str:
    """The actionable next step when git failed for want of a credential; ``""`` otherwise.

    A resolved token still buys nothing unless git's credential helper is wired
    to consume it, so the two cases need different fixes and the raw git error
    distinguishes neither.
    """
    if not any(marker in git_stderr.lower() for marker in CREDENTIAL_FAILURE_MARKERS):
        return ""
    if credential.source is CredentialSource.AMBIENT:
        return (
            "no forge token resolved — export TEATREE_GH_TOKEN (or GH_TOKEN), "
            "or provision the overlay's pass store, then re-run `t3 push`"
        )
    return (
        f"a {credential.source.value} token was supplied but git could not use it — "
        "run `gh auth setup-git` to wire git's credential helper to gh, then re-run `t3 push`"
    )


__all__ = [
    "CREDENTIAL_FAILURE_MARKERS",
    "REDACTION",
    "CredentialSource",
    "ForgeCredential",
    "credential_failure_hint",
    "remote_url_embeds_credential",
    "resolve_forge_credential",
    "scrub_token",
]
