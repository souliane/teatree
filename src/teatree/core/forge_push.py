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
from typing import Self, TypedDict
from urllib.parse import urlsplit

from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.git_run import run as git_read
from teatree.utils.run import CompletedProcess, TimeoutExpired, run_allowed_to_fail

PUSH_TIMEOUT_SECONDS = 300.0

#: The post-condition read is a second network round trip, so it is bounded too — but
#: far tighter than the push: it transfers one ref, never a pack.
VERIFY_TIMEOUT_SECONDS = 60.0

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

#: git's stderr when the remote held the ref and declined the update.
_NON_FAST_FORWARD_MARKERS: tuple[str, ...] = ("non-fast-forward", "fetch first", "[rejected]")

#: git's stderr when the REMOTE's own policy declined the update. Distinct from
#: ``[rejected]`` as a substring, so the two never cross-match.
_REMOTE_REJECTION_MARKERS: tuple[str, ...] = ("[remote rejected]", "hook declined")

#: Lines that prove git got as far as talking to the remote.
_REMOTE_CONTACT_PREFIXES: tuple[str, ...] = ("To ", "remote:")

#: git's summary line after a push it started and could not finish — printed when a
#: local hook aborts it, absent when git died before that (an unresolvable host).
_GIT_PUSH_ABORTED = "error: failed to push some refs"

#: git's own exit code when a pre-push hook refuses. A transport failure exits 128,
#: which is what lets the two be told apart without guessing.
_GATE_REFUSAL_RC = 1

#: git's own outer commentary on a failed push — true of every push failure and
#: therefore evidence of none. Dropping it is what leaves the refusing gate's own
#: words as the message (souliane/teatree#4076).
_GIT_OUTER_PUSH_NOISE: tuple[str, ...] = ("error: failed to push some refs", "hint:", "To ")


class CredentialSource(StrEnum):
    """Where the forge-write credential came from, in resolution order."""

    GH_TOKEN = "GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    TEATREE_GH_TOKEN = "TEATREE_GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    OVERLAY_PASS_STORE = "overlay pass store"  # noqa: S105 — a source label, not a credential
    AMBIENT = "ambient git credential helper"


class PushFailure(StrEnum):
    """Why a push did not deliver, at the granularity the operator's next action needs.

    Every member is a different fix — edit the code, fix the environment, fetch and
    merge, retry — so collapsing them onto one rc=1 costs the caller the diagnosis
    (souliane/teatree#4076). ``NONE`` is falsy, so ``if outcome.failure`` reads.
    """

    NONE = ""
    CONFIG = "config"
    CREDENTIAL = "credential"
    GATE_REFUSED = "gate-refused"
    NON_FAST_FORWARD = "non-fast-forward"
    REMOTE_REJECTED = "remote-rejected"
    TRANSPORT = "transport"
    NOT_ON_REMOTE = "not-on-remote"
    REMOTE_SHA_MISMATCH = "remote-sha-mismatch"
    UNVERIFIABLE = "unverifiable"


#: ``t3 push``'s exit status per failure kind — the machine-readable half of the same
#: distinction, for a caller that branches on the code rather than parsing prose.
PUSH_EXIT_CODES: dict[PushFailure, int] = {
    PushFailure.NONE: 0,
    PushFailure.TRANSPORT: 1,
    PushFailure.CONFIG: 2,
    PushFailure.CREDENTIAL: 3,
    PushFailure.GATE_REFUSED: 4,
    PushFailure.NON_FAST_FORWARD: 5,
    PushFailure.NOT_ON_REMOTE: 6,
    PushFailure.REMOTE_SHA_MISMATCH: 6,
    PushFailure.UNVERIFIABLE: 7,
    PushFailure.REMOTE_REJECTED: 8,
}


@dataclass(frozen=True)
class PushVerdict:
    """One failure kind and the sentence that tells the operator what to do about it."""

    failure: PushFailure
    detail: str


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
    failure: str
    exit_code: int


@dataclass(frozen=True)
class PushOutcome:
    """The result of one :func:`push_branch` call — the sub-agent return contract.

    ``pushed_sha`` is only ever a sha read back off the remote, so on an ``ok``
    outcome it is an observation rather than a restatement of what was asked for.
    """

    ok: bool
    branch: str
    remote: str
    credential_source: CredentialSource
    pushed_sha: str = ""
    detail: str = ""
    failure: PushFailure = PushFailure.NONE

    @property
    def exit_code(self) -> int:
        """The status ``t3 push`` exits with — never 0 while ``ok`` is false.

        A refusal that reached here with no kind set is a bug in the producer, and
        the one resolution that cannot repeat souliane/teatree#4088 is to fail closed.
        """
        if self.ok:
            return 0
        return PUSH_EXIT_CODES[self.failure] or PUSH_EXIT_CODES[PushFailure.TRANSPORT]

    def as_dict(self) -> PushReport:
        return {
            "ok": self.ok,
            "branch": self.branch,
            "remote": self.remote,
            "credential_source": self.credential_source.value,
            "pushed_sha": self.pushed_sha,
            "detail": self.detail,
            "failure": self.failure.value,
            "exit_code": self.exit_code,
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


@dataclass(frozen=True)
class RemoteUrls:
    """A remote's fetch and push urls, which ``remote.<name>.pushurl`` can divorce.

    Both matter, for different reasons: the PUSH url is where the branch actually
    goes and therefore the only endpoint whose answer verifies anything, while a
    credential embedded in EITHER persists in ``.git/config`` and outlives the session.
    ``get-url --push`` falls back to the fetch url when no ``pushurl`` is set.
    """

    fetch: str
    push: str

    @classmethod
    def read(cls, *, repo: str, remote: str) -> Self:
        fetch = git_read(repo=repo, args=["remote", "get-url", remote])
        push = git_read(repo=repo, args=["remote", "get-url", "--push", remote]) if fetch else ""
        return cls(fetch=fetch, push=push)

    @property
    def embeds_credential(self) -> bool:
        return any(remote_url_embeds_credential(url) for url in (self.fetch, self.push))


@dataclass(frozen=True)
class ObservedRemoteRef:
    """What the remote itself answers for one branch — the only evidence a push landed.

    ``git rev-parse <remote>/<branch>`` cannot play this role: it reads the local
    remote-tracking ref, which says what this clone last heard, not what the remote
    holds now (souliane/teatree#4088).
    """

    remote: str
    branch: str
    reachable: bool
    sha: str

    @classmethod
    def observe(cls, *, repo: str, remote: str, branch: str, env: dict[str, str]) -> Self:
        # The remote NAME carries config `ls-remote <url>` would drop (`uploadpack`,
        # `proxy`), so it stays the target unless a `pushurl` makes the two endpoints
        # genuinely different — in which case only the push url proves anything.
        urls = RemoteUrls.read(repo=repo, remote=remote)
        target = urls.push if urls.push and urls.push != urls.fetch else remote
        try:
            result = run_with_status(
                repo=repo,
                args=["ls-remote", target, f"refs/heads/{branch}"],
                env=env,
                timeout=VERIFY_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            return cls(remote=remote, branch=branch, reachable=False, sha="")
        if result.returncode != 0:
            return cls(remote=remote, branch=branch, reachable=False, sha="")
        line = result.stdout.strip()
        return cls(remote=remote, branch=branch, reachable=True, sha=line.split()[0] if line else "")

    def verdict(self, local_sha: str) -> PushVerdict:
        if not self.reachable:
            return PushVerdict(
                PushFailure.UNVERIFIABLE,
                f"git push exited 0 but '{self.remote}' could not be read back, so nothing confirms "
                f"'{self.branch}' landed — treat it as unlanded and re-run `t3 push` once the remote answers",
            )
        if not self.sha:
            return PushVerdict(
                PushFailure.NOT_ON_REMOTE,
                f"git push exited 0 but '{self.remote}' has no refs/heads/{self.branch} — nothing landed",
            )
        if self.sha != local_sha:
            return PushVerdict(
                PushFailure.REMOTE_SHA_MISMATCH,
                f"git push exited 0 but '{self.remote}' holds refs/heads/{self.branch} at {self.sha}, "
                f"not the local tip {local_sha} — fetch and compare before re-running `t3 push`",
            )
        return PushVerdict(PushFailure.NONE, "")


@dataclass(frozen=True)
class GitPushError:
    """A non-zero ``git push``, classified into the failure the operator must act on.

    git reports a refusing pre-push gate, an unusable credential and a stale branch
    through one rc=1 and one outer sentence; each needs a different fix, and only the
    gate case has an author whose own words were worth keeping.
    """

    returncode: int
    stderr: str
    pre_push_hook: str
    credential: ForgeCredential

    @classmethod
    def of(cls, result: CompletedProcess[str], *, repo: str, credential: ForgeCredential) -> Self:
        hook = Path(repo) / git_read(repo=repo, args=["rev-parse", "--git-path", "hooks/pre-push"])
        return cls(
            returncode=result.returncode,
            stderr=(result.stderr or result.stdout).strip(),
            pre_push_hook=str(hook) if os.access(hook, os.X_OK) else "",
            credential=credential,
        )

    @property
    def gate_output(self) -> str:
        kept = [line for line in self.stderr.splitlines() if not line.startswith(_GIT_OUTER_PUSH_NOISE)]
        return "\n".join(kept).strip()

    @property
    def reached_the_remote(self) -> bool:
        return any(line.startswith(_REMOTE_CONTACT_PREFIXES) for line in self.stderr.splitlines())

    @property
    def refused_by_a_gate(self) -> bool:
        """Positive evidence a LOCAL hook aborted the push — never mere absence of evidence.

        Every teatree checkout has an executable pre-push hook, and an unreachable
        network produces no remote-contact lines either, so inferring the gate from
        absence blames it for every transport outage — the same mis-diagnosis
        souliane/teatree#4076 exists to stop. git prints its aborted-push summary only
        for a push it started and could not finish, and exits 1 rather than 128.
        """
        return (
            bool(self.pre_push_hook)
            and self.returncode == _GATE_REFUSAL_RC
            and any(line.startswith(_GIT_PUSH_ABORTED) for line in self.stderr.splitlines())
            and not self.reached_the_remote
        )

    @property
    def failure(self) -> PushFailure:
        lowered = self.stderr.lower()
        if self.refused_by_a_gate:
            return PushFailure.GATE_REFUSED
        if any(marker in lowered for marker in _REMOTE_REJECTION_MARKERS):
            return PushFailure.REMOTE_REJECTED
        if any(marker in lowered for marker in _CREDENTIAL_FAILURE_MARKERS):
            return PushFailure.CREDENTIAL
        if any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS):
            return PushFailure.NON_FAST_FORWARD
        return PushFailure.TRANSPORT

    @property
    def verdict(self) -> PushVerdict:
        failure = self.failure
        if failure is PushFailure.GATE_REFUSED:
            return PushVerdict(failure, self._gate_detail())
        if failure is PushFailure.CREDENTIAL:
            hint = credential_failure_hint(self.stderr, self.credential)
            return PushVerdict(failure, f"git push failed (rc={self.returncode}): {self.stderr} — {hint}")
        if failure is PushFailure.NON_FAST_FORWARD:
            return PushVerdict(
                failure,
                f"the remote branch has commits this clone does not (rc={self.returncode}) — fetch and "
                f"integrate them, then re-run `t3 push`: {self.stderr}",
            )
        if failure is PushFailure.REMOTE_REJECTED:
            return PushVerdict(
                failure,
                f"the remote's own policy declined this update (rc={self.returncode}) — a branch "
                f"protection rule or a server-side hook, which no retry from here changes: {self.stderr}",
            )
        return PushVerdict(failure, f"git push failed (rc={self.returncode}): {self.stderr}")

    def _gate_detail(self) -> str:
        if self.gate_output:
            return (
                f"the pre-push gate refused this push (rc={self.returncode}); "
                f"{self.pre_push_hook} said:\n{self.gate_output}"
            )
        return (
            f"the pre-push gate {self.pre_push_hook} refused this push (rc={self.returncode}) and printed "
            "nothing — a gate killed mid-run (an OOM cap kills the sweep it escalated to) leaves no output. "
            "Run the gate directly to see its stage output"
        )


def _current_branch(repo: str) -> str:
    branch = git_read(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])
    return "" if branch == "HEAD" else branch


def _resolved_branch(repo: str, branch: str) -> str:
    """The plain branch NAME *branch* denotes — the form the rest of this module assumes.

    ``git push`` accepts ``HEAD`` and a fully-qualified ``refs/heads/x`` as well as a
    bare name, but the post-condition has to look up ``refs/heads/<name>`` on the remote
    and would find nothing under either of the other two spellings. Normalising here is
    what keeps them working rather than being refused or, worse, reported unlanded.
    """
    if not branch or branch == "HEAD":
        return _current_branch(repo)
    return branch.removeprefix("refs/heads/")


def _push_argv(repo: str, remote: str, branch: str, *, force_with_lease: bool) -> list[str]:
    argv = ["git", "-C", repo, "push", "--set-upstream", remote, branch]
    if force_with_lease:
        argv.insert(4, "--force-with-lease")
    return argv


def _refusal(verdict: PushVerdict, *, branch: str, remote: str, credential: ForgeCredential) -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch=branch,
        remote=remote,
        credential_source=credential.source,
        detail=scrub_token(verdict.detail, credential.token),
        failure=verdict.failure,
    )


def _local_tip(*, repo: str, branch: str) -> str:
    """The sha *branch* points at, or ``""`` when it resolves nothing.

    ``git rev-parse`` ECHOES an unresolvable argument back on stderr and exits 128, so
    a lenient reader hands back the branch NAME as if it were a sha — the same
    echoed-answer trap souliane/teatree#4088 is about. The return code is the only
    honest signal, so this reads it.
    """
    result = run_with_status(repo=repo, args=["rev-parse", branch])
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_verdict(*, repo: str, remote: str, branch: str) -> PushVerdict:
    """The repo-config reason this push must not even be attempted; ``NONE`` when there is none."""
    if not branch:
        return PushVerdict(
            PushFailure.CONFIG,
            "refusing to push a detached HEAD — check out a branch first, or pass --branch",
        )
    if not _local_tip(repo=repo, branch=f"refs/heads/{branch}"):
        return PushVerdict(
            PushFailure.CONFIG,
            f"no branch '{branch}' in {repo} — check the spelling, or drop --branch to push the "
            "checked-out one. git resolves the refspec before it runs any hook, so this never "
            "reached the pre-push gate",
        )
    urls = RemoteUrls.read(repo=repo, remote=remote)
    if not urls.fetch:
        return PushVerdict(PushFailure.CONFIG, f"no remote named '{remote}' in {repo} — add it, or pass --remote")
    if urls.embeds_credential:
        return PushVerdict(
            PushFailure.CONFIG,
            f"remote '{remote}' embeds a credential in its URL — that secret persists in .git/config "
            f"and outlives the session. Strip it (`git remote set-url {remote} <url-without-credentials>`, "
            "and `--push` too if a pushurl is set) and re-run `t3 push`, which supplies the credential "
            "to git as env only",
        )
    return PushVerdict(PushFailure.NONE, "")


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
    traceback. An rc=0 ``git push`` only ends the attempt; the branch counts as
    pushed once ``git ls-remote`` — a read of the remote, not of any local ref —
    reports it at the local tip. Anything else is a refusal carrying the
    :class:`PushFailure` that says which fix it needs.
    """
    credential = resolve_forge_credential()
    repo_path = str(repo)
    resolved_branch = _resolved_branch(repo_path, branch)
    config = _config_verdict(repo=repo_path, remote=remote, branch=resolved_branch)
    if config.failure:
        return _refusal(config, branch=resolved_branch, remote=remote, credential=credential)

    env = git_env_non_interactive()
    if credential.token:
        env["GH_TOKEN"] = credential.token
    # Read BEFORE the push: a commit landing locally while it runs would otherwise make
    # a genuinely delivered push look like a mismatch against a tip it never carried.
    local_tip = _local_tip(repo=repo_path, branch=resolved_branch)
    try:
        result = run_allowed_to_fail(
            _push_argv(repo_path, remote, resolved_branch, force_with_lease=force_with_lease),
            expected_codes=None,
            env=env,
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except TimeoutExpired:
        return _refusal(
            PushVerdict(PushFailure.TRANSPORT, f"push timed out after {PUSH_TIMEOUT_SECONDS:.0f}s"),
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )
    if result.returncode != 0:
        return _refusal(
            GitPushError.of(result, repo=repo_path, credential=credential).verdict,
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )

    observed = ObservedRemoteRef.observe(repo=repo_path, remote=remote, branch=resolved_branch, env=env)
    landing = observed.verdict(local_tip)
    if landing.failure:
        return _refusal(landing, branch=resolved_branch, remote=remote, credential=credential)

    return PushOutcome(
        ok=True,
        branch=resolved_branch,
        remote=remote,
        credential_source=credential.source,
        pushed_sha=observed.sha,
        detail=scrub_token((result.stderr or result.stdout).strip(), credential.token),
    )


__all__ = [
    "PUSH_EXIT_CODES",
    "PUSH_TIMEOUT_SECONDS",
    "REDACTION",
    "VERIFY_TIMEOUT_SECONDS",
    "CredentialSource",
    "ForgeCredential",
    "GitPushError",
    "ObservedRemoteRef",
    "PushFailure",
    "PushOutcome",
    "PushReport",
    "PushVerdict",
    "RemoteUrls",
    "credential_failure_hint",
    "push_branch",
    "remote_url_embeds_credential",
    "resolve_forge_credential",
    "scrub_token",
]
