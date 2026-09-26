"""Subject-only branch prefilter with an explicit missing-local-ref outcome."""

import re
from dataclasses import dataclass, field

from teatree.utils import git
from teatree.utils.git_run import run_with_status
from teatree.utils.run import CommandFailedError

_PR_SUFFIX_RE = re.compile(r"(?:\s*\(#\d+\))+$")
_RELEASE_NOTE_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*\([^)]+\)\s*$")
_TYPE_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!?:\s*", re.IGNORECASE)
_BRANCH_LOG_FIELDS = 3


@dataclass(frozen=True)
class BranchCommit:
    """A commit on a branch that is not reachable from any remote by SHA."""

    sha: str
    subject: str
    is_merge: bool


@dataclass(frozen=True)
class SubjectPrefilterResult:
    """A subject-only pre-filter of a branch's unsynced commits — NEVER authorizes a destroy.

    The bucketing is by canonicalized SUBJECT membership alone, with no
    content/patch-id/tree check, so it can only *recognize* a likely
    squash-merged candidate cheaply — it must never be the sole gate on a
    destructive action. ``content_equivalence_blockers`` is the authoritative
    content gate every destructive caller passes instead.

    ``squash_merged`` — subject matches a commit on the target branch, so the
    content is *probably* already integrated (typical squash-merge case,
    including the ``relax:`` → ``feat:`` prefix rewrite). A subject collision with
    an unrelated upstream commit lands a genuine commit here — hence pre-filter
    only.

    ``merge_commits`` — commits with multiple parents (Merge branch 'main' into
    feature). They carry no net content of their own and are usually safe to
    discard, but an evil-merge can, so the content gate still has final say.

    ``genuinely_ahead`` — everything else. The branch has work whose subject does
    not appear on the target.

    ``branch_missing`` — the requested local branch ref does not exist. This is
    distinct from a git failure: callers scanning multiple repositories may skip
    a stale worktree row, while every other git error still fails closed.
    """

    squash_merged: list[BranchCommit] = field(default_factory=list)
    merge_commits: list[BranchCommit] = field(default_factory=list)
    genuinely_ahead: list[BranchCommit] = field(default_factory=list)
    branch_missing: bool = False


def _canonicalize_subject(subject: str) -> str:
    """Normalize a commit subject for cross-branch matching.

    Strips, in order: trailing ``(#NNN)`` (added on squash-merge), trailing
    ``[flag] (ticket_url)`` (release-note suffix enforced by the PR-metadata
    hook — present on the merged title but usually absent from the local
    commit), and leading ``type(scope):`` so the ``relax:`` → ``feat(scope):``
    rewrite still matches.
    """
    stripped = _PR_SUFFIX_RE.sub("", subject).strip()
    stripped = _RELEASE_NOTE_SUFFIX_RE.sub("", stripped).strip()
    stripped = _TYPE_PREFIX_RE.sub("", stripped).strip()
    return stripped.lower()


def prefilter_branch_commits_by_subject(
    repo: str,
    branch: str,
    target: str = "origin/main",
) -> SubjectPrefilterResult:
    """Bucket unsynced commits by subject while distinguishing a missing local ref.

    This is a cheap recognizer, NOT an authorizer: a genuine un-upstreamed
    commit whose subject collides with an already-upstreamed subject slips into
    ``squash_merged``, so no destructive caller may act on this result without an
    authoritative content-equivalence check confirming it.

    Both git log calls fail loud. Only a failed branch log followed by
    ``show-ref`` proving the local branch absent becomes ``branch_missing``;
    every other git error still raises ``CommandFailedError``.
    """
    try:
        raw = git.run_strict(
            repo=repo,
            args=["log", branch, "--not", target, "--format=%H%x00%P%x00%s"],
        )
    except CommandFailedError:
        branch_ref = run_with_status(
            repo=repo,
            args=["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        )
        if branch_ref.returncode == 1:
            return SubjectPrefilterResult(branch_missing=True)
        raise
    classification = SubjectPrefilterResult()
    if not raw.strip():
        return classification

    target_raw = git.run_strict(repo=repo, args=["log", target, "--format=%s", "-n", "500"])
    target_subjects = {_canonicalize_subject(line) for line in target_raw.splitlines() if line.strip()}
    target_subjects.discard("")

    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00", 2)
        if len(parts) < _BRANCH_LOG_FIELDS:
            continue
        sha, parents, subject = parts
        is_merge = len(parents.split()) > 1
        commit = BranchCommit(sha=sha, subject=subject, is_merge=is_merge)
        if is_merge:
            classification.merge_commits.append(commit)
        elif _canonicalize_subject(subject) in target_subjects:
            classification.squash_merged.append(commit)
        else:
            classification.genuinely_ahead.append(commit)
    return classification
