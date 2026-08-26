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

from teatree.core.forge_push_refs import BranchRef, local_tip
from teatree.core.push.forge_credential import (
    CREDENTIAL_FAILURE_MARKERS,
    CredentialSource,
    ForgeCredential,
    credential_failure_hint,
    remote_url_embeds_credential,
    resolve_forge_credential,
    scrub_token,
)
from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.git_run import run as git_read
from teatree.utils.run import CompletedProcess, TimeoutExpired, run_allowed_to_fail

#: The single ``git push`` subprocess call runs the WHOLE pre-push hook chain
#: (``dev/push-gate.sh``: the never-lockout contract, ``tests/conformance``, the
#: incremental push gate's scoped doctest + ast-grep) before any byte reaches the
#: network — hooks run as a child of git itself, so one Python-level timeout
#: necessarily covers both phases; splitting them would mean running the hooks a
#: second time standalone and pushing with ``--no-verify``, defeating the "hooks
#: always gate the push" guarantee this module exists for (souliane/teatree#4484).
#: Evidence for the bound: ``dev/push-gate.sh`` alone measured 428s GREEN on a
#: 3-file diff at box load 40; ticket 1015 (#4404, a materially larger diff)
#: independently stalled behind a ~1200s (20min) gate run at similar load. 1800s
#: clears both with headroom while staying a genuinely-enforced, finite bound —
#: a real transport hang is still caught, just not mistaken for a hook chain that
#: is merely slow under load.
PUSH_TIMEOUT_SECONDS = 1800.0

#: The post-condition read is a second network round trip, so it is bounded too — but
#: far tighter than the push: it transfers one ref, never a pack.
VERIFY_TIMEOUT_SECONDS = 60.0

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

    def verdict(self, local_sha: str) -> PushVerdict:
        if not self.reachable:
            return PushVerdict(
                PushFailure.UNVERIFIABLE,
                f"git push exited 0 but '{self.remote}' could not be read back, so nothing confirms "
                f"'{self.branch.name}' landed — treat it as unlanded and re-run `t3 push` once the remote answers",
            )
        if not self.sha:
            return PushVerdict(
                PushFailure.NOT_ON_REMOTE,
                f"git push exited 0 but '{self.remote}' has no {self.branch.qualified} — nothing landed",
            )
        if self.sha != local_sha:
            return PushVerdict(
                PushFailure.REMOTE_SHA_MISMATCH,
                f"git push exited 0 but '{self.remote}' holds {self.branch.qualified} at {self.sha}, "
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
    stdout: str
    pre_push_hook: str
    credential: ForgeCredential

    @classmethod
    def of(cls, result: CompletedProcess[str], *, repo: str, credential: ForgeCredential) -> Self:
        hook = Path(repo) / git_read(repo=repo, args=["rev-parse", "--git-path", "hooks/pre-push"])
        return cls(
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
            stdout=(result.stdout or "").strip(),
            pre_push_hook=str(hook) if os.access(hook, os.X_OK) else "",
            credential=credential,
        )

    @property
    def classification_text(self) -> str:
        """What git itself said — the only stream whose markers classify the failure.

        A hook merely quoting ``authentication failed`` must not be reclassified as
        a credential failure, so stdout is consulted only when git wrote nothing.
        """
        return self.stderr or self.stdout

    @property
    def gate_output(self) -> str:
        """The refusing hook's own words, from BOTH streams (souliane/teatree#4591).

        git leaves a pre-push hook's stdout on ``git push``'s stdout — and prek
        writes its whole failing-hook diagnostic there — while git's own
        aborted-push line always fills stderr. Reading one stream therefore
        discarded exactly the half that named the refusing hook.
        """
        lines = [*self.stdout.splitlines(), *self.stderr.splitlines()]
        return "\n".join(line for line in lines if not line.startswith(_GIT_OUTER_PUSH_NOISE)).strip()

    @property
    def reached_the_remote(self) -> bool:
        return any(line.startswith(_REMOTE_CONTACT_PREFIXES) for line in self.classification_text.splitlines())

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
            and any(line.startswith(_GIT_PUSH_ABORTED) for line in self.classification_text.splitlines())
            and not self.reached_the_remote
        )

    @property
    def failure(self) -> PushFailure:
        lowered = self.classification_text.lower()
        if self.refused_by_a_gate:
            return PushFailure.GATE_REFUSED
        if any(marker in lowered for marker in _REMOTE_REJECTION_MARKERS):
            return PushFailure.REMOTE_REJECTED
        if any(marker in lowered for marker in CREDENTIAL_FAILURE_MARKERS):
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
            hint = credential_failure_hint(self.classification_text, self.credential)
            return PushVerdict(failure, f"git push failed (rc={self.returncode}): {self.classification_text} — {hint}")
        if failure is PushFailure.NON_FAST_FORWARD:
            return PushVerdict(
                failure,
                f"the remote branch has commits this clone does not (rc={self.returncode}) — fetch and "
                f"integrate them, then re-run `t3 push`: {self.classification_text}",
            )
        if failure is PushFailure.REMOTE_REJECTED:
            return PushVerdict(
                failure,
                f"the remote's own policy declined this update (rc={self.returncode}) — a branch "
                f"protection rule or a server-side hook, which no retry from here changes: {self.classification_text}",
            )
        return PushVerdict(failure, f"git push failed (rc={self.returncode}): {self.classification_text}")

    def _gate_detail(self) -> str:
        if self.gate_output:
            return (
                f"the pre-push gate refused this push (rc={self.returncode}); "
                f"{self.pre_push_hook} said:\n{self.gate_output}"
            )
        return (
            f"the pre-push gate {self.pre_push_hook} refused this push (rc={self.returncode}) and printed "
            "nothing on either stream, so its cause is unmeasured. Run the chain directly to see which hook "
            "refused: `uv run prek run --hook-stage pre-push --from-ref origin/main --to-ref HEAD`"
        )


def _push_argv(repo: str, remote: str, branch: BranchRef, *, force_with_lease: bool) -> list[str]:
    argv = ["git", "-C", repo, "push", "--set-upstream", remote, branch.qualified]
    if force_with_lease:
        argv.insert(4, "--force-with-lease")
    return argv


def _refusal(verdict: PushVerdict, *, branch: BranchRef, remote: str, credential: ForgeCredential) -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch=branch.name,
        remote=remote,
        credential_source=credential.source,
        detail=scrub_token(verdict.detail, credential.token),
        failure=verdict.failure,
    )


def _config_verdict(*, repo: str, remote: str, branch: BranchRef) -> PushVerdict:
    """The repo-config reason this push must not even be attempted; ``NONE`` when there is none."""
    if not branch.name:
        return PushVerdict(
            PushFailure.CONFIG,
            "refusing to push a detached HEAD — check out a branch first, or pass --branch",
        )
    if not local_tip(repo=repo, ref=branch.qualified):
        return PushVerdict(
            PushFailure.CONFIG,
            f"no branch '{branch.name}' in {repo} — check the spelling, or drop --branch to push the "
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
    resolved_branch = BranchRef.resolve(repo=repo_path, branch=branch)
    config = _config_verdict(repo=repo_path, remote=remote, branch=resolved_branch)
    if config.failure:
        return _refusal(config, branch=resolved_branch, remote=remote, credential=credential)

    env = git_env_non_interactive()
    if credential.token:
        env["GH_TOKEN"] = credential.token
    # Read BEFORE the push: a commit landing locally while it runs would otherwise make
    # a genuinely delivered push look like a mismatch against a tip it never carried.
    tip_before_push = local_tip(repo=repo_path, ref=resolved_branch.qualified)
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
    "PUSH_EXIT_CODES",
    "PUSH_TIMEOUT_SECONDS",
    "VERIFY_TIMEOUT_SECONDS",
    "GitPushError",
    "ObservedRemoteRef",
    "PushFailure",
    "PushOutcome",
    "PushReport",
    "PushVerdict",
    "RemoteUrls",
    "push_branch",
]
