"""Pre-cold-review / pre-ship branch-currency conflict gate (#940).

The exit-point sibling of :mod:`teatree.core.gates.clone_guard` (#948, the
session-start ``t3 doctor`` clone-currency check). #948 surfaces "a registered
clone is behind its remote"; #940 covers "do not let the cold
reviewer attest, or ``ship`` push, a feature branch whose target has
moved past the branch point". A target-branch move only poisons the
work when the two diverging edits actually *conflict*; a behind-but-
mergeable branch is safe to clear and squash-merge — GitHub re-applies
the branch's diff onto the current target at merge time.

The ship-side gate (:func:`require_current_branch`) checks checkout identity
before fetching the target, then returns one of:

* **refuses** when a pure ``merge-tree`` prediction finds conflicts;
    it names the paths without changing the reviewed head.
* **refuses** when the checkout is not on the recorded branch
    before fetching.
* **no-ops** when the branch is current or behind without conflicts.

The CLEAR-side gate (:func:`sha_conflicts_with_target`) is
**conflict-only**: it predicts — without mutating the worktree, via
``git merge-tree --write-tree`` — whether merging the target into the
reviewed SHA would conflict, and refuses *only* on a real conflict. A
branch that is merely behind but conflict-free is allowed: blocking it
would impose a rebase/update-branch ritual that adds no safety.

A failed fetch is inconclusive — same posture as :mod:`clone_guard`: do
not block when the network is down.
"""

from dataclasses import dataclass
from typing import TypedDict

from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail, run_bounded_group


@dataclass(frozen=True, slots=True)
class BranchStaleness:
    """One stale-branch finding: feature branch behind ``target``."""

    branch: str
    target: str
    behind_count: int
    base_oid: str
    target_oid: str


class BranchCurrencyResult(TypedDict):
    """Public result of :func:`require_current_branch`.

    ``error`` is set when the gate refused (conflict or wrong checkout);
    ``hint`` carries the manual remediation.
    """

    error: str | None
    hint: str | None


def _git(repo: str, *args: str) -> tuple[int, str]:
    """``(returncode, stdout)`` for ``git -C <repo> <args>`` — never raises.

    Runs with the inherited ``GIT_*`` env stripped so a call from inside a git
    hook (which exports ``GIT_DIR``/``GIT_INDEX_FILE`` for the outer repo) still
    targets ``repo``, not the ambient one.
    """
    result = run_allowed_to_fail(
        ["git", "-C", repo, *args],
        expected_codes=None,
        env=git_env_without_overrides(),
    )
    return result.returncode, result.stdout.strip()


PLAN_CURRENCY_FETCH_TIMEOUT_SECONDS = 5.0


def _fetch_target(repo: str, target: str) -> bool:
    """Fetch the remote behind ``target`` (e.g. ``origin`` for ``origin/main``).

    Returns ``True`` on success; a failed fetch (offline, auth) is an
    inconclusive skip — see :mod:`clone_guard` for the same posture.
    """
    remote = target.split("/", 1)[0] if "/" in target else "origin"
    rc, _ = _git(repo, "fetch", remote)
    return rc == 0


def _fetch_target_bounded(repo: str, target: str, *, timeout: float) -> bool:
    remote = target.split("/", 1)[0] if "/" in target else "origin"
    try:
        result = run_bounded_group(
            ["git", "-C", repo, "fetch", remote], timeout=timeout, expected_codes=None, env=git_env_without_overrides()
        )
    except TimeoutExpired:
        return False
    return result.returncode == 0


def _rev_parse(repo: str, ref: str) -> str:
    """Return the resolved SHA for ``ref``; empty string on failure."""
    rc, out = _git(repo, "rev-parse", ref)
    return out if rc == 0 else ""


def _rev_count(repo: str, range_spec: str) -> int:
    """``git rev-list --count <range>`` — 0 on any failure (caller fails open)."""
    rc, out = _git(repo, "rev-list", "--count", range_spec)
    if rc != 0 or not out:
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class MergeConflict:
    """One conflict-only CLEAR-gate finding: the reviewed SHA would not merge.

    ``reviewed_sha`` and ``target`` are behind by ``behind_count``
    commits AND merging the two produces real (textual) conflicts in
    ``conflicting_paths``. A behind-but-mergeable SHA never yields this
    finding — being behind alone is not a merge blocker.
    """

    reviewed_sha: str
    target: str
    behind_count: int
    conflicting_paths: tuple[str, ...]


def predict_merge_conflicts(repo: str, reviewed_sha: str, target: str = "origin/main") -> tuple[str, ...] | None:
    """Predict conflicts of merging ``target`` into ``reviewed_sha``, no mutation.

    Public because a caller that has ALREADY fetched (the testing-phase brief,
    which fetches once through :func:`fetch_target_head`) must be able to predict
    without paying a second fetch, which :func:`sha_conflicts_with_target` would.

    Uses ``git merge-tree --write-tree`` (git ≥ 2.38): a pure object-DB
    merge that never touches the index or worktree, so it is safe to run
    against an arbitrary reviewed SHA while another branch is checked
    out. Per its exit-code protocol, ``0`` ⇒ clean (``()``); ``1`` ⇒
    conflicts, where the output's first line is the tree oid and the
    rest are the conflicting paths; any other code (bad object, old git)
    is inconclusive and returns ``None`` so the caller fails open — same
    posture as a failed fetch.
    """
    rc, out = _git(repo, "merge-tree", "--write-tree", "--name-only", reviewed_sha, target)
    if rc == 0:
        return ()
    if rc != 1:
        return None
    return tuple(line for line in out.splitlines()[1:] if line.strip())


def sha_conflicts_with_target(repo: str, reviewed_sha: str, target: str = "origin/main") -> MergeConflict | None:
    """Return a finding only when ``reviewed_sha`` would *conflict* with ``target``.

    The CLEAR-side, conflict-only gate (#940, relaxed): the reviewed SHA
    is blocked from CLEAR **only** if it both trails ``target`` and the
    merge produces real conflicts an automatic squash-merge could not
    resolve. A branch that is merely behind but conflict-free returns
    ``None`` — it clears and squash-merges without a rebase, because
    GitHub re-applies its diff onto the live target at merge time and
    the merge-time live-CI re-check still guards correctness.

    Inconclusive cases (failed fetch, merge-tree unsupported) return
    ``None`` so the gate fails open — same posture as :mod:`clone_guard`.
    """
    if not _fetch_target(repo, target):
        return None
    behind = _rev_count(repo, f"{reviewed_sha}..{target}")
    if behind <= 0:
        return None
    conflicts = predict_merge_conflicts(repo, reviewed_sha, target)
    if not conflicts:
        return None
    return MergeConflict(
        reviewed_sha=reviewed_sha,
        target=target,
        behind_count=behind,
        conflicting_paths=conflicts,
    )


def fetch_target_head(repo: str, target: str = "origin/main") -> str:
    """Fetch ``target``'s remote and return its live HEAD SHA; ``''`` when inconclusive.

    The plan-currency probe's fetch step (SELFCATCH-3). The fetch is bounded by
    :data:`PLAN_CURRENCY_FETCH_TIMEOUT_SECONDS` and its whole process group is killed at
    the deadline, because the caller can hold the database write lock while it waits.
    An empty return (timeout, failed fetch, unresolvable ref) signals an inconclusive
    probe so the caller fails OPEN — a network outage must not wedge coding.
    """
    if not _fetch_target_bounded(repo, target, timeout=PLAN_CURRENCY_FETCH_TIMEOUT_SECONDS):
        return ""
    return _rev_parse(repo, target)


def commits_between_touching_paths(
    repo: str,
    base_sha: str,
    head_ref: str,
    paths: tuple[str, ...] | list[str],
) -> tuple[str, ...] | None:
    """SHAs in ``base_sha..head_ref`` that modify any of ``paths`` (no fetch).

    The seam-move probe (SELFCATCH-3): ``git log base_sha..head_ref -- <paths>``.
    ``()`` ⇒ the base is current OR no intervening commit touched a declared seam;
    a non-empty tuple ⇒ the plan's base moved and the move touched a seam (the plan
    is stale). ``None`` ⇒ inconclusive (unresolvable ``base_sha``/range, old git)
    so the caller fails OPEN — same posture as :func:`predict_merge_conflicts`. An
    empty ``paths`` short-circuits to ``()`` (a ``no_seams`` plan has nothing to
    guard). The caller is expected to have fetched already.
    """
    if not paths:
        return ()
    rc, out = _git(repo, "log", "--format=%H", f"{base_sha}..{head_ref}", "--", *paths)
    if rc != 0:
        return None
    return tuple(line for line in out.splitlines() if line.strip())


def branch_behind_target(repo: str, branch: str, target: str = "origin/main") -> BranchStaleness | None:
    """Return staleness for ``branch`` vs ``target``, or ``None`` when current.

    Fetches ``target``'s remote first so the comparison reflects the
    remote's real HEAD, not a cached refs snapshot. ``None`` ⇒ the
    target is reachable from the branch tip (branch is current).
    """
    if not _fetch_target(repo, target):
        return None
    branch_ref = f"refs/heads/{branch}"
    behind = _rev_count(repo, f"{branch_ref}..{target}")
    if behind <= 0:
        return None
    base_oid = _rev_parse(repo, branch_ref)
    target_oid = _rev_parse(repo, target)
    return BranchStaleness(
        branch=branch,
        target=target,
        behind_count=behind,
        base_oid=base_oid,
        target_oid=target_oid,
    )


def _checkout_ref(repo: str) -> str:
    rc, ref = _git(repo, "symbolic-ref", "--quiet", "HEAD")
    return ref if rc == 0 else ""


def _ok(*, hint: str | None = None) -> BranchCurrencyResult:
    """Build a non-blocking result (no error)."""
    return BranchCurrencyResult(error=None, hint=hint)


def _wrong_checkout_result(repo: str, branch: str, current_ref: str) -> BranchCurrencyResult:
    current = repr(current_ref.removeprefix("refs/heads/")) if current_ref else "a detached HEAD or unreadable checkout"
    return BranchCurrencyResult(
        error=f"refusing to ship: checkout {repo!r} is on {current}, not recorded branch {branch!r}",
        hint=f"Check out {branch!r} in {repo!r} or correct the Worktree row's checkout path, then retry.",
    )


def require_current_branch(
    repo: str,
    branch: str,
    *,
    target: str = "origin/main",
    dry_run: bool = False,
) -> BranchCurrencyResult:
    """Check branch currency without changing the reviewed head.

    ``dry_run`` reports staleness without checking the checkout —
    used by callers that just want to surface the gap (e.g. a doctor
    check, a defense-in-depth pre-push probe).

    On conflict or wrong checkout the caller must not proceed.
    """
    if not dry_run:
        current_ref = _checkout_ref(repo)
        if current_ref != f"refs/heads/{branch}":
            return _wrong_checkout_result(repo, branch, current_ref)
    staleness = branch_behind_target(repo, branch, target)
    if staleness is None or dry_run:
        hint = (
            f"branch {branch!r} is {staleness.behind_count} commit(s) behind {target}; merge target before review/ship"
            if staleness is not None
            else None
        )
        return _ok(hint=hint)
    paths = predict_merge_conflicts(repo, staleness.base_oid, target)
    if not paths:
        return _ok()
    paths_str = ", ".join(paths)
    return BranchCurrencyResult(
        error=(f"refusing to ship: merging {target} into {branch!r} would conflict in: {paths_str}."),
        hint=(
            f"Run `git merge {target}` on {branch!r}, resolve conflicts, commit, then retry — "
            "the cold reviewer must attest the resolved head."
        ),
    )
