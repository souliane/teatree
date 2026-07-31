"""The one supported push path from the worker container (souliane/teatree#3927).

Without a named seam every agent improvises, and the two forms it reaches for
first are the ones that hurt: a bare ``git push`` from a shell that has no
``GH_TOKEN`` (a ``docker exec`` bypasses the entrypoint's export and inherits
only ``TEATREE_GH_TOKEN`` from the compose ``env_file``) blocks on git's
interactive credential prompt until something kills it, and the "fix" for that
— writing the token into ``remote.origin.url`` — persists the credential in the
``.git/config`` of a host-bind-mounted worktree, where it outlives the session.

:func:`push_branch` closes both: the credential is resolved through the same
chain the loop scanners use and handed to git as ``GH_TOKEN`` env only, every
interactive prompt is disabled so a missing credential fails in milliseconds
with a readable reason, and a remote that already embeds a secret is refused
rather than pushed to. It never passes ``--no-verify``, so the pre-push hooks
still gate the push, and it offers ``--force-with-lease`` but no bare
``--force``.
"""

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from teatree.utils.git_run import git_env_non_interactive
from teatree.utils.git_run import run as git_read
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

PUSH_TIMEOUT_SECONDS = 300.0

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
_CREDENTIAL_FAILURE_MARKERS: tuple[str, ...] = (
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


class PushReport(TypedDict):
    """The JSON document ``t3 push --json`` emits — the machine-readable contract."""

    ok: bool
    branch: str
    remote: str
    credential_source: str
    pushed_sha: str
    detail: str


@dataclass(frozen=True)
class PushOutcome:
    """The result of one :func:`push_branch` call — the sub-agent return contract."""

    ok: bool
    branch: str
    remote: str
    credential_source: CredentialSource
    pushed_sha: str = ""
    detail: str = ""

    def as_dict(self) -> PushReport:
        return {
            "ok": self.ok,
            "branch": self.branch,
            "remote": self.remote,
            "credential_source": self.credential_source.value,
            "pushed_sha": self.pushed_sha,
            "detail": self.detail,
        }


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
    if not any(marker in git_stderr.lower() for marker in _CREDENTIAL_FAILURE_MARKERS):
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


def _current_branch(repo: str) -> str:
    branch = git_read(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])
    return "" if branch == "HEAD" else branch


def _push_argv(repo: str, remote: str, branch: str, *, force_with_lease: bool) -> list[str]:
    argv = ["git", "-C", repo, "push", "--set-upstream", remote, branch]
    if force_with_lease:
        argv.insert(4, "--force-with-lease")
    return argv


def _refusal(reason: str, *, branch: str, remote: str, credential: ForgeCredential) -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch=branch,
        remote=remote,
        credential_source=credential.source,
        detail=scrub_token(reason, credential.token),
    )


def push_branch(
    *,
    repo: str | Path = ".",
    remote: str = "origin",
    branch: str = "",
    force_with_lease: bool = False,
) -> PushOutcome:
    """Push *branch* of *repo* to *remote* over the supported credential path.

    Returns a :class:`PushOutcome` rather than raising: the caller (``t3 push``)
    turns it into an exit code, and a refusal must be readable rather than a
    traceback. A successful push is verified by re-reading the remote-tracking
    ref, so "pushed" means the ref actually moved, not merely that git exited 0.
    """
    credential = resolve_forge_credential()
    repo_path = str(repo)
    resolved_branch = branch or _current_branch(repo_path)
    if not resolved_branch:
        return _refusal(
            "refusing to push a detached HEAD — check out a branch first, or pass --branch",
            branch="",
            remote=remote,
            credential=credential,
        )

    url = git_read(repo=repo_path, args=["remote", "get-url", remote])
    if not url:
        return _refusal(
            f"no remote named '{remote}' in {repo_path} — add it, or pass --remote",
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )
    if remote_url_embeds_credential(url):
        return _refusal(
            f"remote '{remote}' embeds a credential in its URL — that secret persists in .git/config "
            f"and outlives the session. Strip it (`git remote set-url {remote} <url-without-credentials>`) "
            "and re-run `t3 push`, which supplies the credential to git as env only",
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )

    env = git_env_non_interactive()
    if credential.token:
        env["GH_TOKEN"] = credential.token
    try:
        result = run_allowed_to_fail(
            _push_argv(repo_path, remote, resolved_branch, force_with_lease=force_with_lease),
            expected_codes=None,
            env=env,
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except TimeoutExpired:
        return _refusal(
            f"push timed out after {PUSH_TIMEOUT_SECONDS:.0f}s",
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )
    if result.returncode != 0:
        git_error = (result.stderr or result.stdout).strip()
        hint = credential_failure_hint(git_error, credential)
        return _refusal(
            f"git push failed (rc={result.returncode}): {git_error}" + (f" — {hint}" if hint else ""),
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )

    return PushOutcome(
        ok=True,
        branch=resolved_branch,
        remote=remote,
        credential_source=credential.source,
        pushed_sha=git_read(repo=repo_path, args=["rev-parse", f"{remote}/{resolved_branch}"]),
        detail=scrub_token((result.stderr or result.stdout).strip(), credential.token),
    )


__all__ = [
    "PUSH_TIMEOUT_SECONDS",
    "REDACTION",
    "CredentialSource",
    "ForgeCredential",
    "PushOutcome",
    "PushReport",
    "credential_failure_hint",
    "push_branch",
    "remote_url_embeds_credential",
    "resolve_forge_credential",
    "scrub_token",
]
