"""Compare the open PR's head with the sha a push just delivered.

A push can land on the remote ref while the PR keeps showing an older head, so a green
remote-ref read-back is not proof the PR carries the work. ``confirm_pr_head`` reads the
forge's own account of the open PR and says whether it is the pushed sha.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.core.forge_pr_probe import PrProbeOutcome, find_open_pr_for_branch, forge_for_repo, github_open_pr_head


class PrHeadStatus(StrEnum):
    MATCHES = "matches"
    NO_OPEN_PR = "no-open-pr"
    NOT_CHECKED = "not-checked"
    LAGS = "lags"
    UNVERIFIABLE = "unverifiable"


_PASSING = frozenset({PrHeadStatus.MATCHES, PrHeadStatus.NO_OPEN_PR, PrHeadStatus.NOT_CHECKED})


@dataclass(frozen=True, slots=True)
class PrHeadVerdict:
    status: PrHeadStatus
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in _PASSING

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status.value, "detail": self.detail}


def confirm_pr_head(repo: str | Path, branch: str, pushed_sha: str) -> PrHeadVerdict:
    """Return whether the open PR for *branch* is at *pushed_sha*; an unreadable head is never a pass."""
    head = github_open_pr_head(repo, branch)
    if head is not None:
        if head.head == pushed_sha:
            return PrHeadVerdict(PrHeadStatus.MATCHES, f"{head.url} is at {pushed_sha}")
        return PrHeadVerdict(
            PrHeadStatus.LAGS,
            f"{head.url} head is {head.head}, but the push delivered {pushed_sha}; "
            "the PR does not carry the pushed work yet",
        )
    probe = find_open_pr_for_branch(repo, branch)
    if probe.outcome is PrProbeOutcome.NONE:
        return PrHeadVerdict(PrHeadStatus.NO_OPEN_PR, f"no open PR for '{branch}'; nothing to compare")
    if probe.is_found and forge_for_repo(repo) not in {None, "github"}:
        return PrHeadVerdict(PrHeadStatus.NOT_CHECKED, f"{probe.url} is not on GitHub; the PR head was not compared")
    where = probe.url or f"the open PR for '{branch}'"
    return PrHeadVerdict(
        PrHeadStatus.UNVERIFIABLE,
        f"could not read the head of {where}; the push delivered {pushed_sha} and the PR is unconfirmed",
    )
