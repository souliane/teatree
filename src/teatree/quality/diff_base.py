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
from teatree.utils.work_tree import clean_env

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

    Not every merge-shaped operation has one. ``git merge --squash`` records no
    ref for the incoming side, and rebase / cherry-pick / revert set their own
    refs instead. Those all fall back to the branch tip. For rebase and friends
    that is right — there the staged diff IS the operator's own change — but a
    squash merge is a genuine blind spot git gives us no ref to close.
    """
    if _rev_exists("MERGE_HEAD"):
        return DiffBase(ref="MERGE_HEAD", label="the incoming side (theirs)")
    return None


def staged_diff(base: DiffBase, *args: str) -> str | None:
    """The staged diff against *base*, or ``None`` when git could not produce it.

    ``None`` is deliberately NOT ``""``. An empty diff means "nothing changed
    against this base"; a git failure means "unknown". Collapsing the two is how
    a fail-safe primitive becomes a false green in a caller that reads emptiness
    as an answer — so the distinction is carried in the type.

    *args* are appended verbatim, so a caller keeps its own ``--diff-filter`` /
    ``-U0`` / pathspec choices; only the base is decided here.

    :func:`~teatree.utils.work_tree.clean_env` drops ``GIT_DIR``/``GIT_WORK_TREE``
    so a PATHSPEC in *args* is resolved against the real work tree. Git exports
    ``GIT_DIR`` to a hook fired from a linked worktree, and with it set (and no
    work tree named) it treats the CURRENT DIRECTORY as the top — so a caller
    running from a vendored project handed ``-- pyproject.toml`` matched the
    FORK's file, found nothing, and the gate passed on a diff it never read.
    ``GIT_INDEX_FILE`` survives the strip: it names the index being committed.
    """
    result = run_allowed_to_fail(["git", "diff", "--cached", base.ref, *args], expected_codes=None, env=clean_env())
    return result.stdout if result.returncode == 0 else None


def authored_findings[T](
    scan: Callable[[str], Iterable[T]],
    diff_for: Callable[[DiffBase], str | None],
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

    *key* must be BASE-INVARIANT, and choosing it is the subtle part. A finding
    usually carries a line NUMBER, and the same authored line sits at different
    offsets in the two diffs. Worse, a finding's prose can quote the base it was
    measured against (``lowered from 90 to 80`` vs ``from 95 to 80``). Key only
    on the parts that identify the change itself — kind, path, offending text.
    A key that varies with the base makes the intersection empty and SILENTLY
    drops real findings, which is the failure this whole module exists to avoid.

    If the incoming side cannot be read, every finding is reported unfiltered
    rather than dropped: over-reporting is recoverable by a human, a false green
    is not.
    """
    ours = list(scan(diff_for(branch_tip()) or ""))
    theirs = merge_incoming()
    if theirs is None:
        return ours
    theirs_diff = diff_for(theirs)
    if theirs_diff is None:
        return ours
    authored = {key(finding) for finding in scan(theirs_diff)}
    return [finding for finding in ours if key(finding) in authored]
