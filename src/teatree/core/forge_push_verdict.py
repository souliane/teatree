"""Push failure types and classification, separate from push orchestration."""

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

from teatree.core.push_gate_record import GateRunRecord
from teatree.forge_credentials import ForgeTokenState
from teatree.utils.git_run import run as git_read
from teatree.utils.ram_scope import cgroup_v2_oom_kills
from teatree.utils.run import CompletedProcess

_CREDENTIAL_FAILURE_MARKERS: tuple[str, ...] = (
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "authentication failed",
)
_NON_FAST_FORWARD_MARKERS: tuple[str, ...] = ("non-fast-forward", "fetch first", "[rejected]")
_REMOTE_REJECTION_MARKERS: tuple[str, ...] = ("[remote rejected]", "hook declined")
_REMOTE_CONTACT_PREFIXES: tuple[str, ...] = ("To ", "remote:")
_GIT_PUSH_ABORTED = "error: failed to push some refs"
_GATE_REFUSAL_RC = 1
_GIT_OUTER_PUSH_NOISE: tuple[str, ...] = ("error: failed to push some refs", "hint:", "To ")
#: A bare "ABORTED" also matches a failing test NAMED for this feature, which would
#: report a real refusal as "nothing was rejected" — so match the emitted line.
_GATE_ABORT_MARKERS: tuple[str, ...] = (
    "push-gate: ABORTED",
    "crashed while running",
    "replacing crashed worker",
    "Cannot allocate memory",
    "MemoryError",
)


class CredentialSource(StrEnum):
    """Where the forge-write credential came from, in resolution order."""

    GH_TOKEN = "GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    TEATREE_GH_TOKEN = "TEATREE_GH_TOKEN"  # noqa: S105 — an env-var name, not a credential
    OVERLAY_PASS_STORE = "overlay pass store"  # noqa: S105 — a source label, not a credential
    AMBIENT = "ambient git credential helper"


class PushFailure(StrEnum):
    """Why a push did not deliver, at the granularity the operator's next action needs."""

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
    GATE_ABORTED = "gate-aborted"


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
    PushFailure.GATE_ABORTED: 9,
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
    state: ForgeTokenState = ForgeTokenState.TOKEN
    detail: str = ""


def gate_aborted_verdict(
    *, gate_run: GateRunRecord | None, oom_kill_delta: int | None, output: str = ""
) -> PushVerdict:
    record = gate_run.summary if gate_run is not None else "unavailable"
    oom_delta = str(oom_kill_delta) if oom_kill_delta is not None else "unknown"
    output_detail = f" Gate output:\n{output}" if output else ""
    return PushVerdict(
        PushFailure.GATE_ABORTED,
        "the pre-push gate did not reach a verdict; this is an infrastructure failure, not a finding "
        "against the branch. Nothing was verified and nothing was rejected. "
        f"Gate run: {record}. cgroup oom_kill delta: {oom_delta}.{output_detail}",
    )


def credential_failure_hint(git_stderr: str, credential: ForgeCredential) -> str:
    """The actionable next step when git failed for want of a credential; ``""`` otherwise."""
    if not any(marker in git_stderr.lower() for marker in _CREDENTIAL_FAILURE_MARKERS):
        return ""
    if credential.state is not ForgeTokenState.TOKEN:
        return (
            f"no routed forge token resolved ({credential.state.value}: {credential.detail}) — "
            "configure github_token_pass_key for the repository's owning overlay, then re-run `t3 push`"
        )
    return (
        f"a {credential.source.value} token was supplied but git could not use it — "
        "run `gh auth setup-git` to wire git's credential helper to gh, then re-run `t3 push`"
    )


@dataclass(frozen=True)
class GitPushError:
    """A non-zero ``git push``, classified into the failure the operator must act on."""

    returncode: int
    stderr: str
    pre_push_hook: str
    credential: ForgeCredential
    gate_run: GateRunRecord | None = None
    oom_kill_delta: int | None = None

    @classmethod
    def of(
        cls,
        result: CompletedProcess[str],
        *,
        repo: str,
        credential: ForgeCredential,
        since: float,
        oom_kills_before: int | None,
    ) -> Self:
        hook = Path(repo) / git_read(repo=repo, args=["rev-parse", "--git-path", "hooks/pre-push"])
        oom_kills_after = cgroup_v2_oom_kills()
        oom_delta = (
            oom_kills_after - oom_kills_before if oom_kills_before is not None and oom_kills_after is not None else None
        )
        streams = [stream.strip() for stream in (result.stdout, result.stderr) if stream.strip()]
        return cls(
            returncode=result.returncode,
            stderr="\n".join(streams),
            pre_push_hook=str(hook) if os.access(hook, os.X_OK) else "",
            credential=credential,
            gate_run=GateRunRecord.read(repo, since=since),
            oom_kill_delta=oom_delta,
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
    def gate_aborted(self) -> bool:
        lowered = self.stderr.lower()
        marker_found = any(marker.lower() in lowered for marker in _GATE_ABORT_MARKERS)
        record_interrupted = self.gate_run is not None and self.gate_run.was_interrupted
        oom_killed = self.oom_kill_delta is not None and self.oom_kill_delta > 0
        return self.refused_by_a_gate and (not self.gate_output or record_interrupted or oom_killed or marker_found)

    @property
    def failure(self) -> PushFailure:
        lowered = self.stderr.lower()
        if self.gate_aborted:
            return PushFailure.GATE_ABORTED
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
        if failure is PushFailure.GATE_ABORTED:
            return PushVerdict(failure, self._gate_aborted_detail())
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
        return (
            f"the pre-push gate refused this push (rc={self.returncode}); "
            f"{self.pre_push_hook} said:\n{self.gate_output}"
        )

    def _gate_aborted_detail(self) -> str:
        return gate_aborted_verdict(
            gate_run=self.gate_run,
            oom_kill_delta=self.oom_kill_delta,
            output=self.gate_output,
        ).detail


__all__ = [
    "PUSH_EXIT_CODES",
    "CredentialSource",
    "ForgeCredential",
    "GitPushError",
    "PushFailure",
    "PushVerdict",
    "credential_failure_hint",
    "gate_aborted_verdict",
]
