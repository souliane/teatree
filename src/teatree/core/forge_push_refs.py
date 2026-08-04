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
from typing import Self

from teatree.utils.git_run import run_with_status


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
    """One branch in the two spellings git needs, so no call site has to pick."""

    name: str

    @classmethod
    def resolve(cls, *, repo: str, branch: str) -> Self:
        """The branch *branch* denotes, whichever of the three spellings it arrived in.

        ``git push`` accepts ``HEAD`` and a fully-qualified ``refs/heads/x`` as well as
        a bare name; normalising here is what keeps all three working rather than being
        refused or, worse, reported unlanded.
        """
        if not branch or branch == "HEAD":
            return cls(name=_checked_out_branch(repo))
        return cls(name=branch.removeprefix("refs/heads/"))

    @property
    def qualified(self) -> str:
        return f"refs/heads/{self.name}"


def local_tip(*, repo: str, ref: str) -> str:
    """The sha *ref* points at, or ``""`` when it resolves nothing.

    ``git rev-parse`` ECHOES an unresolvable argument back on stderr and exits 128, so
    a lenient reader hands back the ref NAME as if it were a sha — the same
    echoed-answer trap souliane/teatree#4088 is about. The return code is the only
    honest signal, so this reads it.
    """
    result = run_with_status(repo=repo, args=["rev-parse", ref])
    return result.stdout.strip() if result.returncode == 0 else ""


__all__ = ["BranchRef", "local_tip"]
