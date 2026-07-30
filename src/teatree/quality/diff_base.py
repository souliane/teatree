"""Named diff bases for the staged-diff gates (souliane/teatree#3899).

A gate that shells out to a bare ``git diff --cached`` never says which
comparison it wants; it inherits whatever git defaults to. Outside a merge that
default is right — ``--cached`` compares the index against ``HEAD``, which is
exactly the commit being authored. Mid-merge it is wrong, and wrong in the
direction that costs the most: the index holds the merged result, so every line
the *incoming* side contributes reads as a line this commit introduces. The gate
then judges a diff nobody in this commit wrote and refuses the merge over code
the operator inherited.

The fix is not a different bare invocation, it is a *named* one. A commit has one
side outside a merge (:func:`branch_tip`) and two during one (:func:`branch_tip`
plus :func:`merge_incoming`). The operator's own work is what is new against
**both**: a line already present on the incoming side was written by whoever put
it there, whatever the index now says. :func:`authored_findings` is that rule,
expressed once, so each caller states the comparison it intends rather than
leaving the next reader to infer it from an argument list.

Direction of failure: a base that cannot be resolved degrades to scanning
against the branch tip alone — the pre-#3899 behaviour. A gate whose base
resolution breaks must fall back to over-reporting, never to a silent pass.
"""

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

from teatree.utils.run import run_allowed_to_fail

# git's canonical empty tree. The base for a commit with no parent, where
# ``HEAD`` does not resolve and every staged line is by definition authored here.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True)
class DiffBase:
    """One side of the history a staged diff is compared against.

    ``ref`` is what git is handed; ``label`` is what a human is told when the
    gate explains which changes it looked at.
    """

    ref: str
    label: str


def _rev_exists(ref: str) -> bool:
    result = run_allowed_to_fail(["git", "rev-parse", "-q", "--verify", ref], expected_codes=None)
    return result.returncode == 0


def branch_tip() -> DiffBase:
    """The side the operator already had — "ours", the commit being extended.

    Falls back to the empty tree on an unborn ``HEAD`` (the initial commit), so
    the very first commit is still scanned rather than erroring out.
    """
    if _rev_exists("HEAD"):
        return DiffBase(ref="HEAD", label="the branch tip (ours)")
    return DiffBase(ref=EMPTY_TREE, label="the empty tree (no commit yet)")


def merge_incoming() -> DiffBase | None:
    """The incoming side of an in-progress merge, or ``None`` when not merging.

    ``MERGE_HEAD`` exists only between ``git merge`` and the commit that
    concludes it — precisely the window in which the staged diff stops being a
    faithful record of what this commit's author wrote.
    """
    if _rev_exists("MERGE_HEAD"):
        return DiffBase(ref="MERGE_HEAD", label="the incoming side (theirs)")
    return None


def staged_diff(base: DiffBase, *args: str) -> str:
    """The staged diff against *base*, or ``""`` when git refuses.

    *args* are appended verbatim, so a caller keeps its own ``--diff-filter`` /
    ``-U0`` / pathspec choices; only the base is decided here.
    """
    result = run_allowed_to_fail(["git", "diff", "--cached", base.ref, *args], expected_codes=None)
    return result.stdout if result.returncode == 0 else ""


def authored_findings[T](
    scan: Callable[[str], Iterable[T]],
    diff_for: Callable[[DiffBase], str],
    *,
    key: Callable[[T], Hashable] = lambda finding: finding,
) -> list[T]:
    """Run *scan* over the staged diff, keeping only what this commit's author wrote.

    Outside a merge there is one side, so this is the plain staged scan. During a
    merge the scan is repeated against the incoming side and the two are
    intersected on *key*: a finding that survives the comparison against the
    incoming side is new to that side too, i.e. the operator produced it while
    resolving. One that disappears was already on the incoming side and arrived
    with the merge — inherited, not authored.

    *diff_for* is injected rather than built here so a caller keeps its own diff
    shape (``--diff-filter``, ``-U0``, pathspec) and gets both sides rendered by
    the same command — two bases compared like for like. :func:`staged_diff` is
    the obvious thing to build it from.

    *key* exists because a finding usually carries a line NUMBER, and the same
    authored line sits at different offsets in the two diffs. Key on the parts
    that identify the change (path and text), not on where it happens to land.
    """
    ours = list(scan(diff_for(branch_tip())))
    theirs = merge_incoming()
    if theirs is None:
        return ours
    authored = {key(finding) for finding in scan(diff_for(theirs))}
    return [finding for finding in ours if key(finding) in authored]
