"""Pre-cold-review / pre-ship branch-currency auto-merge gate (#940).

The exit-point sibling of :mod:`teatree.core.clone_guard` (#948, the
entry-point pre-investigation gate). #948 covers "do not begin
investigating against a stale repo"; #940 covers "do not let the cold
reviewer attest, or ``ship`` push, a feature branch whose target has
moved past the branch point". A target-branch move only poisons the
work when the two diverging edits actually *conflict*; a behind-but-
mergeable branch is safe to clear and squash-merge — GitHub re-applies
the branch's diff onto the current target at merge time.

The ship-side gate (:func:`require_current_branch`) fetches the target,
then either:

* **auto-merges** the target into the feature branch on a zero-conflict
    fast-forward (``MergeOutcome.ZERO_CONFLICT``); the caller records the
    new HEAD so the cold reviewer attests the *post-merge* SHA.
* **refuses** when the merge would conflict (``MergeOutcome.CONFLICTED``):
    ``git merge --abort`` restores the worktree, the gate names the
    conflicting paths, and returns an actionable hint instead of leaving
    a half-merged tree that the next step would silently push.
* **no-ops** when the branch is already current
    (``MergeOutcome.ALREADY_CURRENT``).

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
from enum import Enum
from typing import TypedDict

from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import run_allowed_to_fail


@dataclass(frozen=True, slots=True)
class BranchStaleness:
    """One stale-branch finding: feature branch behind ``target``."""

    branch: str
    target: str
    behind_count: int
    base_oid: str
    target_oid: str


class MergeOutcome(Enum):
    """Outcome of an attempted auto-merge of ``target`` into the branch."""

    ZERO_CONFLICT = "zero_conflict"
    CONFLICTED = "conflicted"
    FETCH_FAILED = "fetch_failed"
    ALREADY_CURRENT = "already_current"


class BranchCurrencyResult(TypedDict):
    """Public result of :func:`require_current_branch`.

    ``auto_merged`` is ``True`` only when the gate *performed* a
    zero-conflict merge (so the caller knows to re-read HEAD). ``error``
    is set when the gate refused (conflict); ``hint`` carries the
    manual remediation. ``post_merge_sha`` is the new HEAD after a
    successful merge, or ``None`` when no merge ran.
    """

    auto_merged: bool
    post_merge_sha: str | None
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


def _fetch_target(repo: str, target: str) -> bool:
    """Fetch the remote behind ``target`` (e.g. ``origin`` for ``origin/main``).

    Returns ``True`` on success; a failed fetch (offline, auth) is an
    inconclusive skip — see :mod:`clone_guard` for the same posture.
    """
    remote = target.split("/", 1)[0] if "/" in target else "origin"
    rc, _ = _git(repo, "fetch", remote)
    return rc == 0


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


def _merge_tree_conflicts(repo: str, reviewed_sha: str, target: str) -> tuple[str, ...] | None:
    """Predict conflicts of merging ``target`` into ``reviewed_sha``, no mutation.

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
    conflicts = _merge_tree_conflicts(repo, reviewed_sha, target)
    if not conflicts:
        return None
    return MergeConflict(
        reviewed_sha=reviewed_sha,
        target=target,
        behind_count=behind,
        conflicting_paths=conflicts,
    )


def branch_behind_target(repo: str, branch: str, target: str = "origin/main") -> BranchStaleness | None:
    """Return staleness for ``branch`` vs ``target``, or ``None`` when current.

    Fetches ``target``'s remote first so the comparison reflects the
    remote's real HEAD, not a cached refs snapshot. ``None`` ⇒ the
    target is reachable from the branch tip (branch is current).
    """
    if not _fetch_target(repo, target):
        return None
    behind = _rev_count(repo, f"{branch}..{target}")
    if behind <= 0:
        return None
    base_oid = _rev_parse(repo, branch)
    target_oid = _rev_parse(repo, target)
    return BranchStaleness(
        branch=branch,
        target=target,
        behind_count=behind,
        base_oid=base_oid,
        target_oid=target_oid,
    )


@dataclass(frozen=True, slots=True)
class _MergeAttempt:
    """Outcome + conflicting paths from one merge attempt."""

    outcome: MergeOutcome
    conflicting_paths: tuple[str, ...]


def _attempt_merge(repo: str, branch: str, target: str) -> _MergeAttempt:
    """Single-shot merge attempt: returns outcome and conflicting paths.

    On conflict, captures the unmerged paths from ``git diff
    --name-only --diff-filter=U`` BEFORE running ``merge --abort`` so
    the caller can render a precise hint. The abort itself is best-effort
    — its only job is to keep the tree clean for the next gate.
    """
    if not _fetch_target(repo, target):
        return _MergeAttempt(MergeOutcome.FETCH_FAILED, ())
    if _rev_count(repo, f"{branch}..{target}") <= 0:
        return _MergeAttempt(MergeOutcome.ALREADY_CURRENT, ())
    rc, _ = _git(repo, "merge", "--no-edit", target)
    if rc == 0:
        return _MergeAttempt(MergeOutcome.ZERO_CONFLICT, ())
    # Capture conflicts before aborting — the unmerged index is the
    # authoritative source for which files conflicted.
    _, paths_out = _git(repo, "diff", "--name-only", "--diff-filter=U")
    paths = tuple(line for line in paths_out.splitlines() if line.strip())
    # Restore worktree — `merge --abort` is a no-op when no merge is in
    # progress, so safe to call unconditionally.
    _git(repo, "merge", "--abort")
    return _MergeAttempt(MergeOutcome.CONFLICTED, paths)


def auto_merge_target(repo: str, branch: str, target: str = "origin/main") -> MergeOutcome:
    """Attempt to merge ``target`` into the currently-checked-out ``branch``.

    Fast-forward is preferred (the default ``git merge`` posture). On
    conflict the worktree is restored with ``git merge --abort`` —
    never leaves a half-merged tree. The caller decides how to report
    the outcome; this returns a typed verdict only.
    """
    return _attempt_merge(repo, branch, target).outcome


def _ok(
    *,
    auto_merged: bool = False,
    post_merge_sha: str | None = None,
    hint: str | None = None,
) -> BranchCurrencyResult:
    """Build a non-blocking result (no error)."""
    return BranchCurrencyResult(auto_merged=auto_merged, post_merge_sha=post_merge_sha, error=None, hint=hint)


_NON_BLOCKING_OUTCOMES = frozenset({MergeOutcome.ALREADY_CURRENT, MergeOutcome.FETCH_FAILED})


def require_current_branch(
    repo: str,
    branch: str,
    *,
    target: str = "origin/main",
    dry_run: bool = False,
) -> BranchCurrencyResult:
    """Run the auto-merge gate; return the structured result.

    ``dry_run`` reports the staleness without attempting any merge —
    used by callers that just want to surface the gap (e.g. a doctor
    check, a defense-in-depth pre-push probe).

    On zero-conflict merge the caller should record the returned
    ``post_merge_sha`` so the downstream cold reviewer attests the
    post-merge tree. On conflict the caller must NOT proceed — the
    ``error`` + ``hint`` is the actionable refusal.
    """
    staleness = branch_behind_target(repo, branch, target)
    if staleness is None:
        return _ok()
    if dry_run:
        return _ok(
            hint=(
                f"branch {branch!r} is {staleness.behind_count} commit(s) behind "
                f"{target}; merge target before review/ship"
            )
        )
    attempt = _attempt_merge(repo, branch, target)
    if attempt.outcome is MergeOutcome.ZERO_CONFLICT:
        return _ok(auto_merged=True, post_merge_sha=_rev_parse(repo, "HEAD"))
    if attempt.outcome in _NON_BLOCKING_OUTCOMES:
        # Already-current is a true no-op; fetch-failed is an
        # inconclusive skip (same posture as clone_guard).
        return _ok()
    # CONFLICTED.
    paths_str = ", ".join(attempt.conflicting_paths) if attempt.conflicting_paths else "(unknown — see git status)"
    return BranchCurrencyResult(
        auto_merged=False,
        post_merge_sha=None,
        error=(
            f"refusing to ship: merging {target} into {branch!r} produced conflicts in: "
            f"{paths_str}. Worktree restored via `git merge --abort`."
        ),
        hint=(
            f"Run `git merge {target}` on {branch!r}, resolve conflicts, commit, then retry — "
            "the cold reviewer must attest the post-merge SHA so the release pipeline "
            "does not certify a stale base."
        ),
    )
