"""The one ref form ``t3 push`` reads and writes (souliane/teatree#4117).

A tag sharing a branch's name makes every BARE spelling answer something else:
``rev-parse --abbrev-ref HEAD`` answers ``heads/<name>``, ``rev-parse <name>``
answers the TAG's sha, and ``push <remote> <name>`` refuses the refspec as
ambiguous before any hook runs — so the pre-push gate gets blamed for git's own
refusal. :class:`BranchRef` is where that choice is made once: everything git
reads or writes goes through :attr:`BranchRef.qualified`, and the bare
:attr:`BranchRef.name` survives only in the operator-facing report.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from teatree.utils.git_run import run_with_status

#: Per-worktree markers of an operation that stopped with HEAD mid-way.
_OPERATION_MARKERS = {
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "BISECT_LOG": "a bisect",
}


def _checked_out_branch(repo: str) -> str:
    """The checked-out branch's plain name; ``""`` on a detached HEAD.

    ``rev-parse --abbrev-ref HEAD`` and ``symbolic-ref --short HEAD`` both answer the
    disambiguated ``heads/<name>`` once a tag shares the name, and no later lookup
    resolves that spelling. Plain ``symbolic-ref HEAD`` stays fully qualified, and its
    non-zero exit on a detached HEAD is exactly the ``""`` the caller wants.
    """
    result = run_with_status(repo=repo, args=["symbolic-ref", "HEAD"])
    return result.stdout.strip().removeprefix("refs/heads/") if result.returncode == 0 else ""


@dataclass(frozen=True)
class BranchRef:
    """One branch in the two spellings git needs, so no call site has to pick.

    ``from_head`` publishes HEAD under :attr:`name` rather than the local branch of
    that name; ``unsupported`` keeps a refspec form ``t3 push`` refuses, verbatim.
    """

    name: str
    from_head: bool = False
    unsupported: str = ""

    @classmethod
    def resolve(cls, *, repo: str, branch: str) -> Self:
        """The branch *branch* denotes, whichever of the accepted spellings it arrived in.

        ``git push`` accepts ``HEAD`` and a fully-qualified ``refs/heads/x`` as well as
        a bare name; normalising here is what keeps all three working rather than being
        refused or, worse, reported unlanded. ``HEAD:<branch>`` is the one refspec form
        accepted; a delete, force or rename refspec is kept as :attr:`unsupported`.
        """
        if not branch or branch == "HEAD":
            return cls(name=_checked_out_branch(repo))
        if ":" not in branch and not branch.startswith("+"):
            return cls(name=branch.removeprefix("refs/heads/"))
        source, _, destination = branch.partition(":")
        name = destination.removeprefix("refs/heads/")
        if source != "HEAD" or not name or name == "HEAD" or name.startswith("refs/") or ":" in name:
            return cls(name="", unsupported=branch)
        return cls(name=name, from_head=True)

    @property
    def qualified(self) -> str:
        return f"refs/heads/{self.name}"

    @property
    def source(self) -> str:
        """The local ref whose commits the push delivers."""
        return "HEAD" if self.from_head else self.qualified

    @property
    def refspec(self) -> str:
        return f"HEAD:{self.qualified}" if self.from_head else self.qualified


def local_tip(*, repo: str, ref: str) -> str:
    """The sha *ref* points at, or ``""`` when it resolves nothing.

    ``git rev-parse`` ECHOES an unresolvable argument back on stderr and exits 128, so
    a lenient reader hands back the ref NAME as if it were a sha — the same
    echoed-answer trap souliane/teatree#4088 is about. The return code is the only
    honest signal, so this reads it.
    """
    result = run_with_status(repo=repo, args=["rev-parse", ref])
    return result.stdout.strip() if result.returncode == 0 else ""


def is_checkout(repo: str) -> bool:
    return run_with_status(repo=repo, args=["rev-parse", "--git-dir"]).returncode == 0


def head_is_detached(repo: str) -> bool:
    return not _checked_out_branch(repo)


def operation_in_progress(repo: str) -> str:
    """The operation (``"a rebase"``, ...) holding HEAD mid-way, or ``""`` when none is."""
    args = ["rev-parse"] + [arg for marker in _OPERATION_MARKERS for arg in ("--git-path", marker)]
    result = run_with_status(repo=repo, args=args)
    if result.returncode != 0:
        return "an operation git could not report"
    for operation, path in zip(_OPERATION_MARKERS.values(), result.stdout.splitlines(), strict=True):
        if (Path(repo) / path).exists():
            return operation
    return ""


def stranded_head(repo: str) -> str | None:
    """A commit of HEAD's that no branch, tag or remote-tracking ref holds; ``None`` when unreadable."""
    result = run_with_status(repo=repo, args=["rev-list", "-n1", "HEAD", "--not", "--branches", "--tags", "--remotes"])
    return result.stdout.strip() if result.returncode == 0 else None


def feature_branches_at_head(repo: str) -> list[str]:
    result = run_with_status(
        repo=repo, args=["for-each-ref", "--points-at", "HEAD", "--format=%(refname)", "refs/heads/"]
    )
    names = (line.removeprefix("refs/heads/") for line in result.stdout.splitlines())
    return [name for name in names if name not in {"main", "master"}]


__all__ = [
    "BranchRef",
    "feature_branches_at_head",
    "head_is_detached",
    "is_checkout",
    "local_tip",
    "operation_in_progress",
    "stranded_head",
]
