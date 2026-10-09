"""The one supported push path from the worker container (souliane/teatree#3927).

Without a named seam every agent improvises, and the two forms it reaches for
first are the ones that hurt: a bare ``git push`` without an owning-overlay
credential route blocks on git's interactive credential prompt until something
kills it, and the "fix" for that
— writing the token into ``remote.origin.url`` — persists the credential in the
``.git/config`` of a host-bind-mounted worktree, where it outlives the session.

:func:`push_branch` closes both: the credential is resolved from the repository's
owning overlay and handed to git as ``GH_TOKEN`` (GitHub) or ``GITLAB_TOKEN``
(https GitLab) env only, matching the remote's own forge, every
interactive prompt is disabled so a missing credential fails in milliseconds
with a readable reason, and a remote that already embeds a secret is refused
rather than pushed to. It never passes ``--no-verify``, so the pre-push hooks
still gate the push — after :func:`~teatree.core.prek_hook.harden_hooks` has
replaced whatever prek binary another venue baked into the shared hooks — and it
offers ``--force-with-lease`` but no bare ``--force``.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self, TypedDict
from urllib.parse import urlsplit

from teatree.core.forge_push_refs import (
    BranchRef,
    feature_branches_at_head,
    head_is_detached,
    is_checkout,
    local_tip,
    operation_in_progress,
    stranded_head,
)
from teatree.core.forge_push_verdict import PUSH_EXIT_CODES as _PUSH_EXIT_CODES
from teatree.core.forge_push_verdict import CredentialSource as _CredentialSource
from teatree.core.forge_push_verdict import ForgeCredential as _ForgeCredential
from teatree.core.forge_push_verdict import GitPushError as _GitPushError
from teatree.core.forge_push_verdict import PushFailure as _PushFailure
from teatree.core.forge_push_verdict import PushVerdict as _PushVerdict
from teatree.core.forge_push_verdict import gate_aborted_verdict as _gate_aborted_verdict
from teatree.core.forge_push_verdict import hooks_repair_verdict
from teatree.core.push_gate_record import GateRunRecord
from teatree.forge_credentials import ForgeTokenState, resolve_repo_token
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.git_run import run as git_read
from teatree.utils.ram_scope import cgroup_v2_oom_kills
from teatree.utils.run import TimeoutExpired, run_bounded_group

#: The single ``git push`` subprocess call runs the WHOLE pre-push hook chain
#: (``dev/push-gate.sh``: the never-lockout contract, ``tests/conformance``, the
#: incremental push gate's scoped doctest + ast-grep) before any byte reaches the
#: network — hooks run as a child of git itself, so one Python-level timeout
#: necessarily covers both phases; splitting them would mean running the hooks a
#: second time standalone and pushing with ``--no-verify``, defeating the "hooks
#: always gate the push" guarantee this module exists for (souliane/teatree#4484).
#: The hook precedes transfer and receive-pack updates refs atomically, so a deadline
#: can leave an unchanged or fully-landed ref, never a half-pushed ref.
#: Evidence for the bound: ``dev/push-gate.sh`` alone measured 428s GREEN on a
#: 3-file diff at box load 40; ticket 1015 (#4404, a materially larger diff)
#: independently stalled behind a ~1200s (20min) gate run at similar load. 2700s
#: includes the machine-wide lock queue while staying a genuinely-enforced, finite bound —
#: a real transport hang is still caught, just not mistaken for a hook chain that
#: is merely slow under load.
PUSH_TIMEOUT_SECONDS = 2700.0

#: The post-condition read is a second network round trip, so it is bounded too — but
#: far tighter than the push: it transfers one ref, never a pack.
VERIFY_TIMEOUT_SECONDS = 60.0

REDACTION = "<redacted>"

#: Marks a push whose caller opens the PR itself and may hold the control-DB write lock; the ensure-pr hook skips it.
SHIP_PUSH_ENV = "TEATREE_SHIP_PUSH"

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
    credential_source: _CredentialSource
    pushed_sha: str = ""
    detail: str = ""
    failure: _PushFailure = _PushFailure.NONE

    @property
    def exit_code(self) -> int:
        """The status ``t3 push`` exits with — never 0 while ``ok`` is false.

        A refusal that reached here with no kind set is a bug in the producer, and
        the one resolution that cannot repeat souliane/teatree#4088 is to fail closed.
        """
        if self.ok:
            return 0
        return _PUSH_EXIT_CODES[self.failure] or _PUSH_EXIT_CODES[_PushFailure.TRANSPORT]

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


@dataclass(frozen=True)
class _PushAttempt:
    repo: str
    remote: str
    branch: BranchRef
    env: dict[str, str]
    tip_before_push: str
    credential: _ForgeCredential
    started_at: float
    oom_kills_before: int | None


def scrub_token(text: str, token: str) -> str:
    """Replace every occurrence of *token* in *text* with :data:`REDACTION`."""
    return text.replace(token, REDACTION) if token else text


def resolve_forge_credential(repo: str | Path = ".", *, credential: str = "github_token") -> _ForgeCredential:
    """Resolve the owning overlay's routed *credential* token for *repo*.

    *credential* names the routed setting (``"github_token"`` / ``"gitlab_token"``),
    so a caller that already knows the remote's forge can request the matching route
    instead of the GitHub-only default.
    """
    resolution = resolve_repo_token(str(repo), credential=credential)
    return _ForgeCredential(
        token=resolution.token,
        source=_CredentialSource.OVERLAY_PASS_STORE,
        state=resolution.state,
        detail=resolution.detail,
    )


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

    @property
    def credential_forge(self) -> str:
        """The forge whose routed token this push uses; GitLab's token is only ever sent over https."""
        url = self.push or self.fetch
        forge = forge_from_remote(url)
        if forge == "gitlab" and not url.lower().startswith("https://"):
            return ""
        return forge


@dataclass(frozen=True)
class ObservedRemoteRef:
    """What the remote itself answers for one branch — the only evidence a push landed.

    ``git rev-parse <remote>/<branch>`` cannot play this role: it reads the local
    remote-tracking ref, which says what this clone last heard, not what the remote
    holds now (souliane/teatree#4088).
    """

    remote: str
    branch: BranchRef
    reachable: bool
    sha: str

    @classmethod
    def observe(cls, *, repo: str, remote: str, branch: BranchRef, env: dict[str, str]) -> Self:
        # The remote NAME carries config `ls-remote <url>` would drop (`uploadpack`,
        # `proxy`), so it stays the target unless a `pushurl` makes the two endpoints
        # genuinely different — in which case only the push url proves anything.
        urls = RemoteUrls.read(repo=repo, remote=remote)
        target = urls.push if urls.push and urls.push != urls.fetch else remote
        try:
            result = run_with_status(
                repo=repo,
                args=["ls-remote", target, branch.qualified],
                env=env,
                timeout=VERIFY_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            return cls(remote=remote, branch=branch, reachable=False, sha="")
        if result.returncode != 0:
            return cls(remote=remote, branch=branch, reachable=False, sha="")
        line = result.stdout.strip()
        return cls(remote=remote, branch=branch, reachable=True, sha=line.split()[0] if line else "")

    def verdict(self, local_sha: str) -> _PushVerdict:
        if not self.reachable:
            return _PushVerdict(
                _PushFailure.UNVERIFIABLE,
                f"git push exited 0 but '{self.remote}' could not be read back, so nothing confirms "
                f"'{self.branch.name}' landed — treat it as unlanded and re-run `t3 push` once the remote answers",
            )
        if not self.sha:
            return _PushVerdict(
                _PushFailure.NOT_ON_REMOTE,
                f"git push exited 0 but '{self.remote}' has no {self.branch.qualified} — nothing landed",
            )
        if self.sha != local_sha:
            return _PushVerdict(
                _PushFailure.REMOTE_SHA_MISMATCH,
                f"git push exited 0 but '{self.remote}' holds {self.branch.qualified} at {self.sha}, "
                f"not the local tip {local_sha} — fetch and compare before re-running `t3 push`",
            )
        return _PushVerdict(_PushFailure.NONE, "")


def _push_argv(repo: str, remote: str, branch: BranchRef, *, force_with_lease: bool) -> list[str]:
    argv = ["git", "-C", repo, "push", "--set-upstream", remote, branch.refspec]
    if force_with_lease:
        argv.insert(4, "--force-with-lease")
    return argv


def _timeout_output(exc: TimeoutExpired) -> str:
    # The stub types both streams as bytes, but a text-mode run hands back str.
    texts = [
        stream.decode(errors="replace") if isinstance(stream, bytes) else stream for stream in (exc.stdout, exc.stderr)
    ]
    return "\n".join(text.strip() for text in texts if isinstance(text, str) and text.strip())


def _oom_kill_delta(before: int | None) -> int | None:
    after = cgroup_v2_oom_kills()
    return after - before if before is not None and after is not None else None


def _refusal(verdict: _PushVerdict, *, branch: BranchRef, remote: str, credential: _ForgeCredential) -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch=branch.name,
        remote=remote,
        credential_source=credential.source,
        detail=scrub_token(verdict.detail, credential.token),
        failure=verdict.failure,
    )


def _push_timeout_outcome(exc: TimeoutExpired, attempt: _PushAttempt) -> PushOutcome:
    observed = ObservedRemoteRef.observe(
        repo=attempt.repo,
        remote=attempt.remote,
        branch=attempt.branch,
        env=attempt.env,
    )
    if observed.reachable and observed.sha == attempt.tip_before_push:
        return PushOutcome(
            ok=True,
            branch=attempt.branch.name,
            remote=attempt.remote,
            credential_source=attempt.credential.source,
            pushed_sha=observed.sha,
            detail=f"push deadline hit after landing; remote read-back confirmed {observed.sha}",
        )
    partial_output = _timeout_output(exc)
    gate_run = GateRunRecord.read(attempt.repo, since=attempt.started_at)
    if gate_run is not None and gate_run.was_interrupted:
        verdict = _gate_aborted_verdict(
            gate_run=gate_run,
            oom_kill_delta=_oom_kill_delta(attempt.oom_kills_before),
            output=partial_output,
        )
    else:
        verdict = _PushVerdict(_PushFailure.TRANSPORT, f"push timed out after {PUSH_TIMEOUT_SECONDS:.0f}s")
    return _refusal(
        verdict,
        branch=attempt.branch,
        remote=attempt.remote,
        credential=attempt.credential,
    )


def _detached_head_detail(repo: str) -> str:
    detail = "refusing to push a detached HEAD — pass --branch HEAD:<branch> to publish HEAD as <branch>"
    on_head = feature_branches_at_head(repo)
    return f"{detail}, or `t3 push --branch {on_head[0]}`" if len(on_head) == 1 else detail


def _stranding_detail(repo: str, branch: BranchRef) -> str:
    """Why pushing *branch* from a detached HEAD would leave HEAD's own commits behind; ``""`` if it would not."""
    if branch.from_head or not head_is_detached(repo):
        return ""
    stranded = stranded_head(repo)
    if stranded is None:
        held = "could not be read for a commit no branch holds"
    elif stranded:
        held = f"carries commit {stranded} that no branch holds"
    else:
        return ""
    return (
        f"HEAD (detached) {held}; pushing '{branch.name}' would not deliver it — publish HEAD with "
        f"`t3 push --branch HEAD:{branch.name}`, or keep it with `git branch <name> HEAD`"
    )


def _ref_verdict(*, repo: str, branch: BranchRef) -> _PushVerdict:
    """The local-ref reason this push must not even be attempted; ``NONE`` when there is none."""
    if not is_checkout(repo):
        return _PushVerdict(
            _PushFailure.CONFIG,
            f"'{repo}' is not a git checkout — run t3 push from inside one, or pass --repo <path>",
        )
    if branch.unsupported:
        return _PushVerdict(
            _PushFailure.CONFIG,
            f"refspec '{branch.unsupported}' is not supported — HEAD:<branch> is the only refspec form t3 push takes",
        )
    if not branch.name:
        return _PushVerdict(_PushFailure.CONFIG, _detached_head_detail(repo))
    operation = operation_in_progress(repo) if branch.from_head else ""
    if operation:
        return _PushVerdict(
            _PushFailure.CONFIG, f"{operation} is in progress, so HEAD is mid-way — finish or abort it first"
        )
    if not local_tip(repo=repo, ref=branch.source):
        return _PushVerdict(
            _PushFailure.CONFIG,
            f"no branch '{branch.name}' in {repo} — check the spelling, or drop --branch to push the "
            "checked-out one. git resolves the refspec before it runs any hook, so this never "
            "reached the pre-push gate",
        )
    stranding = _stranding_detail(repo, branch)
    return _PushVerdict(_PushFailure.CONFIG, stranding) if stranding else _PushVerdict(_PushFailure.NONE, "")


def _config_verdict(*, repo: str, remote: str, branch: BranchRef) -> _PushVerdict:
    """The repo-config reason this push must not even be attempted; ``NONE`` when there is none."""
    ref = _ref_verdict(repo=repo, branch=branch)
    if ref.failure:
        return ref
    urls = RemoteUrls.read(repo=repo, remote=remote)
    if not urls.fetch:
        return _PushVerdict(_PushFailure.CONFIG, f"no remote named '{remote}' in {repo} — add it, or pass --remote")
    if urls.embeds_credential:
        return _PushVerdict(
            _PushFailure.CONFIG,
            f"remote '{remote}' embeds a credential in its URL — that secret persists in .git/config "
            f"and outlives the session. Strip it (`git remote set-url {remote} <url-without-credentials>`, "
            "and `--push` too if a pushurl is set) and re-run `t3 push`, which supplies the credential "
            "to git as env only",
        )
    return _PushVerdict(_PushFailure.NONE, "")


def _forge_write_credential(repo_path: str, forge: str) -> _ForgeCredential:
    """The repository's routed write credential for *forge*.

    An https GitLab remote routes through its own ``gitlab_token_pass_key``; any
    other remote (GitHub, SSH, plain http, or a host :func:`~teatree.utils.forge.forge_from_remote`
    cannot classify) keeps the original single-credential GitHub route.
    """
    if forge == "gitlab":
        return resolve_forge_credential(repo_path, credential="gitlab_token")
    return resolve_forge_credential(repo_path)


def _credential_env(forge: str, token: str) -> dict[str, str]:
    return {"GITLAB_TOKEN" if forge == "gitlab" else "GH_TOKEN": token} if token else {}


def forge_write_env(repo: str) -> dict[str, str]:
    """The env that hands *repo*'s routed write credential to git for ``origin``, as ``t3 push`` does."""
    forge = RemoteUrls.read(repo=repo, remote="origin").credential_forge
    return _credential_env(forge, _forge_write_credential(repo, forge).token)


def _refuse_missing_forge_credential(
    forge: str, credential: _ForgeCredential, *, resolved_branch: BranchRef, remote: str
) -> PushOutcome | None:
    """The refusal when *forge* is a known forge with no routed token; ``None`` otherwise.

    An unrecognised host is never refused here — it relies on the ambient git
    credential helper, same as before either forge got an explicit route.
    """
    if forge not in {"github", "gitlab"} or credential.state is ForgeTokenState.TOKEN:
        return None
    return _refusal(
        _PushVerdict(
            _PushFailure.CREDENTIAL,
            f"{forge}_token_pass_key for the repository's owning overlay is "
            f"{credential.state.value}: {credential.detail}; refusing ambient git/gh authentication",
        ),
        branch=resolved_branch,
        remote=remote,
        credential=credential,
    )


def _refuse_unrepairable_hooks(
    repo: str, *, resolved_branch: BranchRef, remote: str, credential: _ForgeCredential
) -> PushOutcome | None:
    repair = hooks_repair_verdict(repo)
    if not repair.failure:
        return None
    return _refusal(repair, branch=resolved_branch, remote=remote, credential=credential)


def push_branch(
    *,
    repo: str | Path = ".",
    remote: str = "origin",
    branch: str = "",
    force_with_lease: bool = False,
    ship_opens_pr: bool = False,
) -> PushOutcome:
    """Push *branch* of *repo* to *remote* over the supported credential path.

    Returns a :class:`PushOutcome` rather than raising: the caller (``t3 push``)
    turns it into an exit code, and a refusal must be readable rather than a
    traceback. An rc=0 ``git push`` only ends the attempt; the branch counts as
    pushed once ``git ls-remote`` — a read of the remote, not of any local ref —
    reports it at the local tip. Anything else is a refusal carrying the
    :class:`~teatree.core.forge_push_verdict.PushFailure` that says which fix it needs.
    """
    repo_path = str(repo)
    resolved_branch = BranchRef.resolve(repo=repo_path, branch=branch)
    config = _config_verdict(repo=repo_path, remote=remote, branch=resolved_branch)
    forge = RemoteUrls.read(repo=repo_path, remote=remote).credential_forge
    credential = _forge_write_credential(repo_path, forge)
    if config.failure:
        return _refusal(config, branch=resolved_branch, remote=remote, credential=credential)
    refusal = _refuse_missing_forge_credential(forge, credential, resolved_branch=resolved_branch, remote=remote)
    if refusal is None:
        refusal = _refuse_unrepairable_hooks(
            repo_path, resolved_branch=resolved_branch, remote=remote, credential=credential
        )
    if refusal is not None:
        return refusal
    env = git_env_non_interactive() | _credential_env(forge, credential.token)
    env.pop(SHIP_PUSH_ENV, None)
    if ship_opens_pr:
        env[SHIP_PUSH_ENV] = "1"
    # Read BEFORE the push: a commit landing locally while it runs would otherwise make
    # a genuinely delivered push look like a mismatch against a tip it never carried.
    tip_before_push = local_tip(repo=repo_path, ref=resolved_branch.source)
    push_started_at = float(int(time.time()))
    oom_kills_before = cgroup_v2_oom_kills()
    attempt = _PushAttempt(
        repo=repo_path,
        remote=remote,
        branch=resolved_branch,
        env=env,
        tip_before_push=tip_before_push,
        credential=credential,
        started_at=push_started_at,
        oom_kills_before=oom_kills_before,
    )
    try:
        result = run_bounded_group(
            _push_argv(repo_path, remote, resolved_branch, force_with_lease=force_with_lease),
            expected_codes=None,
            env=env,
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except TimeoutExpired as exc:
        return _push_timeout_outcome(exc, attempt)
    if result.returncode != 0:
        return _refusal(
            _GitPushError.of(
                result,
                repo=repo_path,
                credential=credential,
                since=push_started_at,
                oom_kills_before=oom_kills_before,
            ).verdict,
            branch=resolved_branch,
            remote=remote,
            credential=credential,
        )

    observed = ObservedRemoteRef.observe(repo=repo_path, remote=remote, branch=resolved_branch, env=env)
    landing = observed.verdict(tip_before_push)
    if landing.failure:
        return _refusal(landing, branch=resolved_branch, remote=remote, credential=credential)

    return PushOutcome(
        ok=True,
        branch=resolved_branch.name,
        remote=remote,
        credential_source=credential.source,
        pushed_sha=observed.sha,
        detail=scrub_token((result.stderr or result.stdout).strip(), credential.token),
    )


__all__ = [
    "PUSH_TIMEOUT_SECONDS",
    "REDACTION",
    "SHIP_PUSH_ENV",
    "VERIFY_TIMEOUT_SECONDS",
    "ObservedRemoteRef",
    "PushOutcome",
    "PushReport",
    "RemoteUrls",
    "forge_write_env",
    "push_branch",
    "remote_url_embeds_credential",
    "resolve_forge_credential",
    "scrub_token",
]
