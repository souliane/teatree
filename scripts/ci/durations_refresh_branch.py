"""Build the durations-refresh branch without discarding what is already on it (#4717).

The refresh used to `git checkout -B` its stable branch off main every run, so the branch was
REBUILT rather than updated: any commit already on it was dropped and `--force-with-lease`
pushed the rebuild over it. The lease does not help — it guards a CONCURRENT push, not this
job deliberately replacing the branch's own content. Measured on #4655: the source fixes that
cleared the PR's review hold were byte-erased and the PR reverted to looking like nobody had
ever addressed the findings, so it was not merely unmergeable but unfixable in place.

It bites here specifically because refreshing the durations is what CREATES the collateral —
a fresh cassette reveals tests now running under their pinned ceilings, and clearing that hold
needs source edits outside the generated file. The one branch that must carry hand fixes was
the one branch that erased them.

Two modes, decided from the branch's own diff against the base:

- FRESH — no remote branch, or its only change is the generated file. Rebuilding loses
    nothing and keeps the single-commit history the stable-branch design (CI-3) wants.
- PRESERVED — the branch carries something else. The base is merged UNDER the existing
    commits and the fresh durations are layered on top, so the hand fixes stay reachable.

A conflict outside the generated file is REFUSED (merge aborted, exit 1, nothing pushed): the
whole point is that a human's fix is never silently discarded, and guessing a resolution is
how it would be. A conflict confined to the generated file is not a decision at all — the
freshly measured cassette is the answer by construction.
"""

import argparse
import dataclasses
import subprocess
import sys
from pathlib import Path

DEFAULT_GENERATED = "dev/.test_durations"
DEFAULT_MESSAGE = "chore(ci): refresh dev/.test_durations from the shard lane (#3160)"
_LS_REMOTE_NO_MATCH = 2


class GitError(RuntimeError):
    """A git invocation this script cannot proceed past."""


@dataclasses.dataclass(frozen=True)
class RefreshBranch:
    """Where the refreshed durations are being published, and from what base."""

    repo: Path
    branch: str
    base_sha: str
    generated: str
    message: str

    @property
    def generated_path(self) -> Path:
        return self.repo / self.generated


@dataclasses.dataclass(frozen=True)
class BranchPlan:
    mode: str
    preserved: list[str]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)


def _git_strict(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        detail = f"`git {' '.join(args)}` exited {result.returncode}: {result.stderr.strip()}"
        raise GitError(detail)
    return result.stdout


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """Whether origin carries the branch — an unreadable remote raises rather than reading empty."""
    probe = _git(repo, "ls-remote", "--exit-code", "--heads", "origin", branch)
    if probe.returncode == 0:
        return True
    if probe.returncode == _LS_REMOTE_NO_MATCH:
        return False
    detail = (
        f"could not read origin's branches ({probe.returncode}): {probe.stderr.strip()}. Refusing to "
        f"treat an unreadable remote as an absent branch, which would rebuild {branch} over whatever "
        f"it carries."
    )
    raise GitError(detail)


def non_generated_changes(repo: Path, *, base: str, ref: str, generated: str) -> list[str]:
    """The paths the branch changed versus the base, minus the file this job regenerates."""
    raw = _git_strict(repo, "diff", "--name-only", f"{base}...{ref}", "--", ".", f":(exclude){generated}")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def unmerged_paths(repo: Path) -> list[str]:
    raw = _git_strict(repo, "diff", "--name-only", "--diff-filter=U")
    return sorted({line.strip() for line in raw.splitlines() if line.strip()})


def _preserved_paths(target: RefreshBranch) -> list[str]:
    if not remote_branch_exists(target.repo, target.branch):
        return []
    refspec = f"+refs/heads/{target.branch}:refs/remotes/origin/{target.branch}"
    _git_strict(target.repo, "fetch", "--quiet", "origin", refspec)
    return non_generated_changes(
        target.repo,
        base=target.base_sha,
        ref=f"origin/{target.branch}",
        generated=target.generated,
    )


def _merge_base_under_branch(target: RefreshBranch, *, snapshot: bytes, preserved: list[str]) -> None:
    if _git(target.repo, "merge", "--no-ff", "--no-edit", target.base_sha).returncode == 0:
        return
    conflicts = unmerged_paths(target.repo)
    if conflicts != [target.generated]:
        _git(target.repo, "merge", "--abort")
        detail = (
            f"merging the base into {target.branch} conflicts outside the generated file, on: "
            f"{', '.join(conflicts) or '<nothing unmerged — the merge failed for another reason>'}. "
            f"The branch carries hand-edited work ({', '.join(preserved)}) that this job must not "
            f"resolve by guessing, so nothing was pushed. Fix the branch by hand, or land those "
            f"fixes on the base branch and delete {target.branch} so the next run rebuilds it."
        )
        raise GitError(detail)
    _stage_generated(target, snapshot)
    _git_strict(target.repo, "commit", "--no-edit")


def _stage_generated(target: RefreshBranch, snapshot: bytes) -> None:
    target.generated_path.write_bytes(snapshot)
    _git_strict(target.repo, "add", "--", target.generated)


def build_branch(target: RefreshBranch, snapshot: bytes) -> BranchPlan:
    """Point the refresh branch at the fresh durations, keeping any other commit already on it."""
    preserved = _preserved_paths(target)
    if preserved:
        _git_strict(target.repo, "checkout", "--quiet", "-B", target.branch, f"origin/{target.branch}")
        _merge_base_under_branch(target, snapshot=snapshot, preserved=preserved)
    else:
        _git_strict(target.repo, "checkout", "--quiet", "-B", target.branch, target.base_sha)

    _stage_generated(target, snapshot)
    if _git(target.repo, "diff", "--cached", "--quiet").returncode != 0:
        _git_strict(target.repo, "commit", "-m", target.message)
    return BranchPlan(mode="preserved" if preserved else "fresh", preserved=preserved)


def _report(plan: BranchPlan) -> None:
    print(f"mode={plan.mode}")
    if plan.preserved:
        joined = " ".join(plan.preserved)
        print(f"preserved_paths={joined}")
        print(f"::notice::Kept {len(plan.preserved)} hand-edited path(s) already on the refresh branch: {joined}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the durations-refresh branch, preserving hand fixes on it.")
    parser.add_argument("--branch", required=True, help="the stable refresh branch to build")
    parser.add_argument("--repo", type=Path, default=Path(), help="repository to act in (default: cwd)")
    parser.add_argument("--base", default="HEAD", help="the base the refresh commit sits on (default: HEAD)")
    parser.add_argument("--generated", default=DEFAULT_GENERATED, help="the file this job regenerates")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="commit message for the regenerated file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    repo: Path = args.repo
    source = repo / args.generated
    if not source.is_file():
        print(f"::error::{source} does not exist, so there are no fresh durations to publish.", file=sys.stderr)
        return 1
    snapshot = source.read_bytes()

    try:
        # Resolve before switching branches: the base is what HEAD names NOW, and the
        # working-tree edit is discarded only because `snapshot` already holds its bytes.
        base_sha = _git_strict(repo, "rev-parse", f"{args.base}^{{commit}}").strip()
        _git_strict(repo, "checkout", base_sha, "--", args.generated)
        target = RefreshBranch(
            repo=repo,
            branch=args.branch,
            base_sha=base_sha,
            generated=args.generated,
            message=args.message,
        )
        plan = build_branch(target, snapshot)
    except GitError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    _report(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
