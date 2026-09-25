"""Hand-written LoC accounting — the one arithmetic behind every net-LoC surface.

Two consumers read it: the advisory CI report (``scripts/ci/loc_ratchet.py``) and
the ``net_hand_written_loc`` factory signal. Sharing the exclusion set is the
point — two implementations of "which lines were hand-written" would print two
different numbers for one tree, which is the redundant-surface class of bug the
LoC direction ticket exists to remove.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path

from teatree.utils.run import run_checked

#: git's own name for the empty tree — the base a window reaching the repo's first
#: commit diffs against, since that commit has no parent to name.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_NUMSTAT_FIELDS = 3
_GENERATED_GLOBS = ("*.bundle", "*.patch")
_GENERATED_DIRS = ("docs/generated", "migrations")
_GENERATED_FILES = ("uv.lock", "config/defaults.toml", "evals/README.md")


@dataclass(frozen=True, slots=True)
class LocDelta:
    added: int
    deleted: int
    commits: int = 0

    @property
    def net(self) -> int:
        return self.added - self.deleted

    def __str__(self) -> str:
        return f"hand-written LoC +{self.added} -{self.deleted} = net {self.net:+d}"


def is_generated(path: str) -> bool:
    """True for a path no human hands wrote, by glob/directory/name alone."""
    if any(fnmatch(path, glob) for glob in _GENERATED_GLOBS):
        return True
    if any(f"/{directory}/" in f"/{path}" for directory in _GENERATED_DIRS):
        return True
    return any(path == name or path.endswith(f"/{name}") for name in _GENERATED_FILES)


def _git(repo: Path, *args: str, stdin_text: str | None = None) -> str:
    return run_checked(["git", "-C", str(repo), *args], stdin_text=stdin_text).stdout


def _attribute_generated(repo: Path, paths: Sequence[str]) -> set[str]:
    """The subset of *paths* git's own ``.gitattributes`` resolution marks ``merge=generated``.

    Asking git rather than re-globbing keeps ``.gitattributes`` the single source:
    a nested one (``vendor/teatree/.gitattributes``) applies to its own subtree with
    no path rewriting here. Paths go over stdin because a whole-window diff names
    tens of thousands of them, well past the argv limit; under ``-z`` git reads
    them NUL-separated, not by line.
    """
    if not paths:
        return set()
    fields = _git(repo, "check-attr", "-z", "--stdin", "merge", stdin_text="\0".join(paths)).split("\0")
    return {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "generated"}


def _tally(repo: Path, numstat: str, commits: int) -> LocDelta:
    rows = []
    for line in numstat.splitlines():
        fields = line.split("\t")
        if len(fields) == _NUMSTAT_FIELDS and fields[0] != "-":
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
    generated = _attribute_generated(repo, [path for _, _, path in rows])
    hand_written = [row for row in rows if row[2] not in generated and not is_generated(row[2])]
    return LocDelta(
        added=sum(added for added, _, _ in hand_written),
        deleted=sum(deleted for _, deleted, _ in hand_written),
        commits=commits,
    )


def diff_range_loc(base: str, head: str = "HEAD", *, repo: Path) -> LocDelta:
    """The hand-written delta over the merge-base range ``base...head``."""
    return _tally(repo, _git(repo, "diff", "--numstat", f"{base}...{head}"), commits=0)


def window_loc(*, since: datetime, until: datetime, repo: Path) -> LocDelta:
    """The hand-written delta between the trees at the window's two boundaries.

    Keyed on the current tip so the memo expires the moment history moves: a report
    read costs one boundary diff, and re-reading an unchanged tree costs nothing.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    return _window_loc(str(repo), tip, since.isoformat(), until.isoformat())


def _boundary(repo: Path, when: str) -> str:
    """The newest commit on HEAD's history at or before *when* — the window edge's tree.

    Never the oldest commit the window CONTAINS: a branch merged inside the window
    carries commits whose parent predates it by months, so a parent-of-oldest base
    diffs a span far wider than the window it claims to measure.
    """
    return _git(repo, "rev-list", "-1", f"--before={when}", "HEAD").strip()


@lru_cache(maxsize=16)
def _window_loc(repo: str, tip: str, since: str, until: str) -> LocDelta:  # noqa: ARG001 — `tip` is the key that expires this entry
    root = Path(repo)
    end = _boundary(root, until)
    if not end:
        return LocDelta(0, 0, 0)
    start = _boundary(root, since)
    commits = int(_git(root, "rev-list", "--count", f"{start}..{end}" if start else end))
    if not commits:
        return LocDelta(0, 0, 0)
    return _tally(root, _git(root, "diff", "--numstat", start or EMPTY_TREE, end), commits=commits)


__all__ = ["EMPTY_TREE", "LocDelta", "diff_range_loc", "is_generated", "window_loc"]
