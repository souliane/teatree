"""PR-side patch since a reviewed ancestor, with explicit full-review fallbacks."""

import shlex
from dataclasses import dataclass
from pathlib import Path

from teatree.utils import git
from teatree.utils.git_run import run_with_status
from teatree.utils.run import CommandFailedError

DELTA_FILE_SHARE = 0.5
BRIEF_PATCH_CHARS = 24_000
_DIFF_PATH_FIELDS = 2


@dataclass(frozen=True, slots=True)
class ReviewDelta:
    case: str
    reason: str = ""
    prior_head: str = ""
    patch: str = ""
    files: frozenset[str] = frozenset()
    pr_files: frozenset[str] = frozenset()

    @property
    def changed_lines(self) -> int:
        return sum(
            line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in self.patch.splitlines()
        )


def _patch_files(patch: str) -> frozenset[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        fields = shlex.split(line.removeprefix("diff --git "))
        if len(fields) == _DIFF_PATH_FIELDS and fields[1].startswith("b/"):
            paths.add(fields[1][2:])
    return frozenset(paths)


def _base_fallback(prior_heads: list[str], base_ref: str, recorded_base_ref: str) -> str:
    if not prior_heads:
        return "no prior verdict"
    if recorded_base_ref and base_ref != recorded_base_ref:
        return "base changed since the prior review"
    return ""


def resolve_delta(
    repo: Path | str, prior_heads: list[str], head: str, base_ref: str, recorded_base_ref: str
) -> ReviewDelta:
    if reason := _base_fallback(prior_heads, base_ref, recorded_base_ref):
        return ReviewDelta("full", reason)
    root = str(repo)
    try:
        prior = ""
        for sha in prior_heads:
            if sha == head:
                continue
            check = run_with_status(repo=root, args=["merge-base", "--is-ancestor", sha, head])
            if check.returncode == 0:
                prior = sha
                break
            if check.returncode != 1:
                return ReviewDelta("full", "git could not read the prior head")
        if not prior:
            return ReviewDelta("full", "prior head is not an ancestor")
        patch = git.run_strict_verbatim(
            repo=root,
            args=["log", "--first-parent", "--reverse", "-p", "--remerge-diff", "--format=", f"{prior}..{head}"],
        )
        files = _patch_files(patch)
        pr_files = frozenset(
            path
            for path in git.run_strict_verbatim(
                repo=root, args=["diff", "-z", "--name-only", f"{base_ref}...{head}"]
            ).split("\0")
            if path
        )
        if pr_files and len(files) / len(pr_files) > DELTA_FILE_SHARE:
            return ReviewDelta("full", "delta touches more than 50% of PR files", prior_head=prior)
        return ReviewDelta("delta", prior_head=prior, patch=patch, files=files, pr_files=pr_files)
    except (OSError, CommandFailedError) as exc:
        return ReviewDelta("full", f"git could not produce the delta ({type(exc).__name__})")
