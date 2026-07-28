"""Which clones teatree knows, and which checkouts their git registries hold (#3852).

The single answer to "does a live checkout own this path?", shared by the two
reapers that ask it. Splitting that question in half is what made ``clean-all``
unsafe to run: the raw-orphan reaper asked git, while the isolated env-dir reaper
asked only the ``Worktree`` table — yet
:func:`teatree.paths.resolve_data_dir` mints an env dir for ANY checkout, whether
teatree registered it or not. The two ends of one deterministic slug mapping
therefore disagreed about the population, and every live-but-unregistered
checkout's control DB was reported as an orphan.

:class:`CheckoutRegistry` carries the gaps alongside the answer on purpose. A
clone whose registry could not be read leaves an unknown number of live checkouts
unaccounted for, and an unaccounted checkout is indistinguishable from a dead
one — so a caller with a destructive disposition must fail CLOSED on a non-empty
``gaps`` rather than delete on partial evidence (the #706 standard).
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.utils import git
from teatree.utils.run import CommandFailedError


@dataclass(frozen=True, slots=True)
class CheckoutRegistry:
    """Every checkout path git reports across the known clones, plus what went unread."""

    paths: frozenset[str]
    gaps: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.gaps


def raw_worktree_paths(repo: str) -> dict[str, str]:
    """Return ``{worktree_path: branch}`` for every LINKED worktree of ``repo``.

    Parses ``git worktree list --porcelain``. The main checkout (the record whose
    path is ``repo`` itself) is excluded — only the linked worktrees are
    candidates. A detached worktree carries no ``branch`` line; it is recorded
    with the literal ``HEAD`` (:data:`git.DETACHED_HEAD`).
    """
    raw = git.run(repo=repo, args=["worktree", "list", "--porcelain"])
    main = str(Path(repo).resolve())
    result: dict[str, str] = {}
    current_path = ""
    current_branch = ""
    for line in [*raw.splitlines(), "worktree "]:  # trailing sentinel flushes the last record
        if line.startswith("worktree "):
            if current_path and str(Path(current_path).resolve()) != main:
                result[current_path] = current_branch or git.DETACHED_HEAD
            current_path = line.removeprefix("worktree ")
            current_branch = ""
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
    return result


def candidate_clones(workspace: Path) -> set[str]:
    """The main clones whose worktree registries may hold orphaned worktrees.

    A worktree's registry lives in its source clone, so orphans are found by
    listing each known main clone's worktrees. The known clones are the
    ``clone_path`` of every ``Worktree`` row (where sub-agents branch from) plus
    the current working directory when it is itself a main clone (``.git`` is a
    directory, not the gitdir-pointer file a linked worktree carries).
    """
    clones: set[str] = set()
    for wt in Worktree.objects.all():
        clone = resolve_clone_path(workspace, wt)
        if clone is not None and (clone / ".git").is_dir():
            clones.add(str(clone.resolve()))
    cwd = Path.cwd()
    if (cwd / ".git").is_dir():
        clones.add(str(cwd.resolve()))
    return clones


def live_checkout_paths(workspace: Path) -> CheckoutRegistry:
    """Every checkout path git can still resolve across :func:`candidate_clones`.

    Each clone contributes itself plus its linked worktrees. Including the clone
    is belt-and-braces — a primary clone resolves to the canonical data dir, never
    an isolated one — but costs nothing and keeps the answer literally "every
    checkout git reports". A clone whose registry raises is recorded as a gap, not
    silently dropped: an unread clone is missing evidence, not absent checkouts.
    """
    found: set[str] = set()
    gaps: list[str] = []
    for repo in sorted(candidate_clones(workspace)):
        try:
            worktrees = raw_worktree_paths(repo)
        except CommandFailedError as exc:
            gaps.append(f"clone {repo}: could not list worktrees ({exc})")
            continue
        found.add(repo)
        found.update(worktrees)
    return CheckoutRegistry(frozenset(found), tuple(gaps))


__all__ = ["CheckoutRegistry", "candidate_clones", "live_checkout_paths", "raw_worktree_paths"]
