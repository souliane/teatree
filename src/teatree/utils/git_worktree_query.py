"""Read-only worktree inspection: canonical-repo resolution + porcelain parse.

The query partition of :mod:`teatree.utils.git`'s worktree surface — the
non-mutating counterpart to :mod:`teatree.utils.git_worktree` (which owns
add/remove/move and the teardown data-loss guards). Everything here answers a
question about an existing worktree ("which clone is this", "which worktree
holds branch X", "is this path a checkout") via the permissive
:mod:`teatree.utils.git_run` runners; none of it is ever the basis of a delete
decision.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.utils.git_run import run


def git_common_dir(path: str | Path) -> Path | None:
    """Resolve the shared ``.git`` common dir of the clone *path* belongs to.

    ``git rev-parse --git-common-dir`` points at the shared ``.git`` regardless
    of which linked worktree *path* names, so it identifies the canonical clone
    even when the worktree directory's basename is not the repo name. The
    command returns a **relative** path when invoked from the repo root, so the
    result is joined against *path* before ``resolve()`` — the detail each
    re-implementation gets wrong once. Returns ``None`` for a non-git path
    (uses the permissive :func:`run`, which yields empty output on failure).
    """
    out = run(repo=str(path), args=["rev-parse", "--git-common-dir"])
    if not out:
        return None
    common = Path(out)
    if not common.is_absolute():
        common = Path(path) / common
    return common.resolve()


def canonical_repo_root(path: str | Path) -> Path | None:
    """The working-tree root of the canonical clone *path* belongs to.

    ``<common-dir>.parent`` — the directory holding the shared ``.git``. Lets a
    consumer classify a worktree by its real clone instead of by
    ``Path(path).name``, which is only *conventionally* the repo name (a
    hand-made ``git worktree add`` yields a branch-named directory). Returns
    ``None`` for a non-git path.
    """
    common = git_common_dir(path)
    return common.parent if common is not None else None


def is_git_checkout(path: str | Path) -> bool:
    """Return ``True`` when *path* is a real checkout (clone OR linked worktree).

    A clone carries ``.git`` as a **directory**; a linked worktree carries it as
    a **file** (a gitdir pointer). The test is therefore
    ``(path / ".git").exists()`` — NOT ``is_dir()`` — so a torn-down "hollow"
    directory (generated artifacts left behind, no ``.git``) is correctly
    rejected rather than mistaken for the real clone.
    """
    return (Path(path) / ".git").exists()


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    """One record parsed from ``git worktree list --porcelain``."""

    path: Path
    head: str
    branch: str  # "" when detached or bare
    detached: bool
    bare: bool
    locked: bool
    prunable: bool


def list_worktrees(repo: str = ".") -> list[WorktreeRecord]:
    """Parse ``git worktree list --porcelain`` for *repo* into structured records.

    The single structured parse of this format — :func:`locked_worktree_paths`
    and the reconciler's path scan both derive from it, so a new consumer never
    writes a fourth bespoke parse. Permissive by design (uses the error-swallowing
    :func:`teatree.utils.git_run.run`): a non-git directory yields an empty list
    rather than raising, matching what the reconciler needs. **Never** use it as
    the basis of a delete decision — the teardown data-loss guards stay on
    ``run_strict`` precisely so an indeterminate answer blocks teardown.

    Porcelain records are blank-line-separated blocks; within a block each
    attribute is its own line (``worktree <path>``, ``HEAD <sha>``,
    ``branch refs/heads/<name>``, or a bare ``bare``/``detached``/``locked``/
    ``prunable`` flag, the last two optionally carrying a reason).
    """
    records: list[WorktreeRecord] = []
    fields: dict[str, str | bool] = {}

    def flush() -> None:
        path = fields.get("path")
        if isinstance(path, str):
            records.append(
                WorktreeRecord(
                    path=Path(path),
                    head=str(fields.get("head", "")),
                    branch=str(fields.get("branch", "")),
                    detached=bool(fields.get("detached")),
                    bare=bool(fields.get("bare")),
                    locked=bool(fields.get("locked")),
                    prunable=bool(fields.get("prunable")),
                )
            )
        fields.clear()

    for line in run(repo=repo, args=["worktree", "list", "--porcelain"]).splitlines():
        if not line:
            flush()
            continue
        keyword, _, rest = line.partition(" ")
        if keyword == "worktree":
            fields["path"] = rest
        elif keyword == "HEAD":
            fields["head"] = rest
        elif keyword == "branch":
            fields["branch"] = rest.removeprefix("refs/heads/")
        elif keyword in {"bare", "detached", "locked", "prunable"}:
            fields[keyword] = True
    flush()
    return records


def worktree_for_branch(repo: str, branch: str) -> WorktreeRecord | None:
    """The worktree that has *branch* checked out, or ``None``.

    Answers "does this branch already have a worktree" (the create-guard) and
    "which worktree holds branch X" (DB↔git reconciliation) off the single
    :func:`list_worktrees` parse. *branch* may be given bare or fully-qualified
    (``refs/heads/…``); both compare equal.
    """
    wanted = branch.removeprefix("refs/heads/")
    for record in list_worktrees(repo):
        if record.branch == wanted:
            return record
    return None
