"""Canonical layered merged-detection for a branch's CURRENT tip.

A branch is REDUNDANT (auto-deletable) only when its CURRENT tip is provably,
fully captured on the target — never on a forge "merged" signal alone. The
detection is three explicit layers, in escalating order, every one CONTENT-based:

cherry-zero — ``git cherry <target> <branch>`` shows no ``+`` line: every unique
commit's patch is already upstream. :func:`content_equivalence_blockers` is the
fail-closed form (it also blocks on a unique merge commit and on any git error).

synthetic-squash (b) — the git-delete-squashed canonical squash detector:
``git cherry <target> $(git commit-tree <branch^{tree}> -p $(git merge-base
<target> <branch>) -m _)``. A leading ``-`` means the branch's WHOLE current
tree-delta is already on ``<target>`` as one squashed patch. This is what
recognises a squash-merge: ``git log <branch> --not <target>`` detects commits by
SHA, but a squash-merge rewrites them into a NEW SHA, so a per-commit /
is-ancestor / three-dot test misses it.

branch-merged (c) — ``git branch --merged <target>`` lists the branch: a plain
merge commit whose tip is an ancestor of the target.

The forge (``gh pr list --state merged`` / ``glab mr list --merged``) is
CORROBORATING ONLY — :func:`_branch_pr_is_merged` reports it for the emit/route
decision, but it NEVER alone authorises a delete (the same invariant the
worktree reaper enforces in :mod:`teatree.core.worktree.worktree_done`). A forge-merged
branch whose current tip still carries content not on the target is classified
NOT-redundant and tagged ``merged_with_post_merge_work`` so the salvage skill
routes that delta to a FRESH PR rather than the CLI silently destroying it.

Comparing against ``origin/main`` (not ``--remotes``) is essential — ``--remotes``
would also exclude the feature branch's own remote tracking ref, hiding commits
that are pushed but not yet on main.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from teatree.utils import git
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

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
    destructive action. :func:`content_equivalence_blockers` is the authoritative
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
    """

    squash_merged: list[BranchCommit] = field(default_factory=list)
    merge_commits: list[BranchCommit] = field(default_factory=list)
    genuinely_ahead: list[BranchCommit] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RedundancyVerdict:
    """The canonical layered verdict on whether a branch's CURRENT tip is redundant.

    ``redundant`` is ``True`` only when one of the three CONTENT layers proved the
    current tip fully captured on the target (auto-delete authorised). The forge
    signal never sets it. ``forge_merged`` is the corroborating forge report.
    ``unique_shas`` are the commits whose patch content is NOT provably on the
    target (the per-commit ``git cherry`` ``+`` SHAs plus any unique merge
    commit); they are the delta the salvage skill routes to a fresh PR when the
    branch is kept. ``source`` names the deciding layer:
    ``cherry-zero-unique`` / ``synthetic-squash`` / ``branch-merged`` /
    ``not-redundant`` / ``inconclusive``.
    """

    redundant: bool
    forge_merged: bool
    unique_shas: list[str] = field(default_factory=list)
    source: str = "not-redundant"

    @property
    def merged_with_post_merge_work(self) -> bool:
        """The forge says merged, yet the current tip carries content not on target.

        The post-merge-work emit tag: the branch shipped a PR/MR but has since
        grown (or never squashed-down) unique content, so it is NOT redundant and
        its ``unique_shas`` are NEW work bound for a fresh PR — never wiped on the
        stale merged signal.
        """
        return self.forge_merged and not self.redundant and bool(self.unique_shas)


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


def prefilter_branch_commits_by_subject(repo: str, branch: str, target: str = "origin/main") -> SubjectPrefilterResult:
    """Subject-only PRE-FILTER of the branch's unsynced commits — NEVER authorizes a destroy.

    Buckets into squash-merged / merge / genuinely-ahead by canonicalized SUBJECT
    alone. This is a cheap recognizer, NOT an authorizer: a genuine un-upstreamed
    commit whose subject collides with an already-upstreamed subject slips into
    ``squash_merged``, so no destructive caller may act on this result without the
    authoritative :func:`content_equivalence_blockers` content gate confirming it.

    Runs two git log invocations: one to list branch commits not on any remote
    (same as :func:`git.unsynced_commits`), one to fetch subjects on ``target``
    for subject matching. Both use :func:`git.run_strict` — a real git failure
    (e.g. ``repo`` is not a filesystem path to a checkout, such as a forge
    slug like ``owner/repo`` passed where a path is expected) raises
    :class:`CommandFailedError` instead of returning empty output, which used
    to be indistinguishable from "branch has no unsynced commits" and
    misclassified a genuinely-ahead branch as synced (#2937).
    """
    raw = git.run_strict(
        repo=repo,
        args=["log", branch, "--not", target, "--format=%H%x00%P%x00%s"],
    )
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


_FALLBACK_DEFAULT_TARGET = "origin/main"


def effective_default_target(repo: str) -> str:
    """Resolve ``repo``'s REAL default branch as an ``origin/<default>`` ref.

    The content/redundancy probes must compare against the repo's ACTUAL default
    branch, not a hardcoded ``origin/main`` — a ``master``/``develop``-default
    repo measured against a base it does not have makes ``git cherry`` fail (or,
    worse, silently mis-measure). Shared here (a leaf both :mod:`cleanup` and
    :mod:`worktree_done` import) so the two teardown paths resolve the base the
    SAME way without an import cycle.

    Fail-safe to ``origin/main`` on an unresolvable default: the downstream
    content gate fails CLOSED (an unresolvable target makes ``git cherry``
    inconclusive → a blocker → refuse), so a wrong/missing base keeps the branch
    rather than wiping it.
    """
    try:
        default = git.default_branch(repo)
    except (RuntimeError, CommandFailedError):
        return _FALLBACK_DEFAULT_TARGET
    return f"origin/{default}"


def content_equivalence_blockers(repo: str, branch: str, target: str = "origin/main") -> list[str]:
    """Return the commit(s) on ``branch`` NOT provably content-equivalent to ``target``.

    The AUTHORITATIVE content gate every destructive caller must pass before
    destroying ``branch`` (#2609). :func:`prefilter_branch_commits_by_subject`
    buckets by canonicalized SUBJECT alone — fine to *recognize* a forge-squash-merged
    candidate, but unsafe to *authorize* a destroy: a genuine un-upstreamed
    commit whose subject collides with an already-upstreamed subject (a routine
    ``docs: update skills``), an amended commit that added content after the
    original squash, or a merge commit carrying unique content all slip past it.
    This proves equivalence by CONTENT instead, so an empty list is positive
    proof that destroying ``branch`` loses nothing.

    Two authoritative checks, both contributing blockers. ``git cherry <target>
    <branch>`` compares each unique commit by **patch-id** (content), not SHA or
    subject: a ``-`` prefix means the patch already landed upstream
    (squash-merge), a ``+`` prefix means it is genuinely un-upstreamed — the ``+``
    sha(s) are blockers. ``git rev-list --merges <target>..<branch>`` lists merge
    commits unique to the branch; a merge commit can carry content in neither
    parent (an evil-merge) and has no single patch-id ``git cherry`` can compare,
    so any merge commit in the unique range blocks conservatively.

    **Fails CLOSED.** A failed ``git cherry`` / ``git rev-list`` (unresolvable
    target, corrupt repo, any git error) is inconclusive — the helper returns an
    opaque ``"(... inconclusive)"`` blocker so the caller REFUSES the destroy.
    Destruction requires a POSITIVE proof of content-equivalence; ambiguity never
    authorizes it.
    """
    blockers: list[str] = []
    try:
        cherry = git.run_strict(repo=repo, args=["cherry", target, branch])
    except CommandFailedError:
        return ["(git cherry failed — content check inconclusive)"]
    blockers.extend(line[1:].strip() for line in cherry.splitlines() if line.strip().startswith("+"))
    try:
        merges = git.run_strict(repo=repo, args=["rev-list", "--merges", f"{target}..{branch}"])
    except CommandFailedError:
        return [*blockers, "(git rev-list --merges failed — merge check inconclusive)"]
    blockers.extend(sha.strip() for sha in merges.splitlines() if sha.strip())
    return blockers


def _pr_merge_commit_sha(repo: str, branch: str) -> str:
    """Return the SHA of the merge/squash commit for ``branch``'s merged PR, or ``""``.

    Queries GitHub (``gh pr list``) and GitLab (``glab mr list``) for a merged
    PR whose source branch matches. The merge commit's tree captures the
    branch's net content at merge time — used by :func:`_branch_tree_matches_squash`
    to distinguish post-merge follow-up commits already captured by the squash
    from commits that add new content.

    Returns ``""`` when neither CLI is available (sandbox, CI without auth) —
    the caller falls back to subject-match classification.
    """
    sha = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "mergeCommit", "--limit", "1"],
        repo,
        lambda data: data[0]["mergeCommit"]["oid"],
    )
    if sha:
        return sha
    return probe_host_cli(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"],
        repo,
        lambda data: data[0]["merge_commit_sha"],
    )


def probe_host_cli(cmd: list[str], repo: str, extract: Callable[[Any], str], *, timeout: float = 30.0) -> str:
    """Invoke a host CLI that may be missing, parse its JSON, extract the SHA.

    Swallows ``OSError`` (missing binary, permission denied in sandboxes) and
    JSON/key errors — both are legitimate "no merged PR found" outcomes.

    ``timeout`` bounds the host CLI invocation (seconds): a hung ``gh``/``glab``
    must not block ``clean-all`` or the loop tick. On expiry the
    ``subprocess.TimeoutExpired`` is swallowed and ``""`` is returned — the same
    fail-safe "not found / skip" value as every other failure path, so a timeout
    can never produce a positive merged signal and never wrongly reaps work.
    """
    try:
        result = run_allowed_to_fail(cmd, cwd=repo, expected_codes=None, timeout=timeout)
    except (OSError, TimeoutExpired):
        return ""
    if result.returncode != 0 or result.stdout.strip() in {"", "[]"}:
        return ""
    try:
        data = json.loads(result.stdout)
        sha = extract(data) if data else ""
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return ""
    return sha or ""


def _branch_pr_is_merged(repo: str, branch: str) -> bool:
    """Whether the forge canonically reports ``branch``'s PR/MR as merged (#1578).

    The subject-match classifier and :func:`_branch_tree_matches_squash` both
    break down for branches that diverged long before they were squash-merged:
    the squash creates a new SHA on the default branch (so no subject matches and
    the branch's own SHAs are absent from every remote) and the branch tip tree
    no longer equals the squash commit tree (main moved on). Such a worktree is
    fully merged yet looks ``genuinely_ahead`` / "commits on NO remote", so the
    guards refuse it forever.

    This asks the forge directly — the canonical truth, not a heuristic. A merged
    PR/MR whose source branch matches ``branch`` means the work shipped, however
    far the local branch has since diverged. GitHub marks a squash-merged PR
    ``state=merged``; GitLab marks the MR ``merged`` — both are covered by the
    same ``--state merged`` / ``--merged`` queries the squash-commit probe uses,
    so this reuses :func:`probe_host_cli` (which swallows a missing ``gh``/``glab``
    binary and any parse error as "not found").

    **Fail-safe to skip.** Returns ``True`` only on a positive merged signal;
    every uncertain outcome (no merged PR, CLI absent, probe/JSON failure) returns
    ``False`` so the caller keeps the conservative refuse-and-report — ambiguity
    never reaps real work.
    """
    found = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        repo,
        lambda data: str(data[0]["number"]),
    )
    if found:
        return True
    found = probe_host_cli(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"],
        repo,
        lambda data: str(data[0]["iid"]),
    )
    return bool(found)


def _branch_has_open_pr(repo: str, branch: str) -> bool:
    """Whether the forge reports an OPEN PR/MR whose source branch is ``branch`` (#3093).

    The squash-merged content heuristic (:func:`is_squash_merged`) matches whenever a
    branch's current tip is patch-id-equivalent to ``origin/<default>`` — which is also
    true for a still-OPEN PR whose branch merely resembles the default branch. Classifying
    such a worktree ``done (squash-merged)`` is a false-done signal a sweep can act on to
    wipe live work. An open PR is the forge's positive proof the work is unfinished, so the
    reaper consults this before trusting the content heuristic.

    **Fail-safe to False.** Returns ``True`` only on a positive open-PR signal; every
    uncertain outcome (no open PR, CLI absent, probe/JSON failure) returns ``False``. It
    only ever ADDS a keep — a genuinely squash-merged branch with no open PR is still
    reaped, and the content-based :func:`analyze_worktree_changes` remains the fail-closed
    data-loss guard when the forge cannot answer.
    """
    found = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "--limit", "1"],
        repo,
        lambda data: str(data[0]["number"]),
    )
    if found:
        return True
    found = probe_host_cli(
        ["glab", "mr", "list", "--source-branch", branch, "--state", "opened", "--output", "json", "-P", "1"],
        repo,
        lambda data: str(data[0]["iid"]),
    )
    return bool(found)


def _branch_tree_matches_squash(repo: str, branch: str) -> bool:
    """Return ``True`` when the PR's merge commit has the same tree as the branch tip.

    Post-merge follow-up commits (retro, docs) appear as ``genuinely_ahead``
    because their subjects don't match the squash commit's final message.
    When their cumulative effect is already captured in the squash tree, the
    branch is safe to clean despite the unmatched subjects.
    """
    merge_sha = _pr_merge_commit_sha(repo, branch)
    if not merge_sha:
        return False
    return git.check(repo=repo, args=["diff", "--quiet", merge_sha, branch])


def _tree_delta_captured(repo: str, ref: str, target: str) -> bool:
    """The git-delete-squashed canonical squash detector — layer (b).

    Builds a SYNTHETIC commit whose tree is ``ref``'s CURRENT tree, parented at
    ``git merge-base <target> <ref>``, then asks ``git cherry <target>
    <synthetic>`` whether that single squashed patch already landed on
    ``<target>``. A leading ``-`` means the branch's WHOLE current tree-delta is
    captured on the target as one squash commit ⇒ fully redundant. This is the
    layer that recognises a squash-merge that a per-commit / is-ancestor test
    misses, AND distinguishes a clean squash from one that grew post-merge work
    (the larger current tree-delta no longer matches the squash patch → ``+``).

    Fails CLOSED: any git error (unresolvable ref/target, corrupt repo) reads as
    NOT captured, so destruction is never authorised on an inconclusive probe.
    Works on any committish ``ref`` — a branch name, ``HEAD``, or a ``stash@{N}``.
    """
    try:
        merge_base = git.run_strict(repo=repo, args=["merge-base", target, ref])
        tree = git.run_strict(repo=repo, args=["rev-parse", f"{ref}^{{tree}}"])
        synthetic = git.run_strict(repo=repo, args=["commit-tree", tree, "-p", merge_base, "-m", "_"])
        cherry = git.run_strict(repo=repo, args=["cherry", target, synthetic])
    except CommandFailedError:
        return False
    lines = [line for line in cherry.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("-") for line in lines)


def branch_redundancy(repo: str, branch: str, target: str = "origin/main") -> RedundancyVerdict:
    """The canonical layered verdict: is ``branch``'s CURRENT tip provably on ``target``?

    Three CONTENT layers decide ``redundant`` (see the module docstring):
    cherry-zero (:func:`content_equivalence_blockers` empty), synthetic-squash
    (:func:`_tree_delta_captured`), then ``git branch --merged``. The forge
    (:func:`_branch_pr_is_merged`) is read for ``forge_merged`` but NEVER enters
    the redundancy decision — a forge-merged tip still carrying unique content is
    returned NOT-redundant with that content in ``unique_shas`` and surfaced via
    :attr:`RedundancyVerdict.merged_with_post_merge_work`.

    Fail-CLOSED on an inconclusive content probe: an erroring ``git cherry``
    (``content_equivalence_blockers`` returns a parenthesised marker) skips the
    squash/merged layers and returns NOT-redundant, so an uncertain branch is
    kept, never deleted.
    """
    forge_merged = _branch_pr_is_merged(repo, branch)
    blockers = content_equivalence_blockers(repo, branch, target)
    unique_shas = [b for b in blockers if not b.startswith("(")]
    inconclusive = any(b.startswith("(") for b in blockers)
    if not blockers:
        return RedundancyVerdict(redundant=True, forge_merged=forge_merged, source="cherry-zero-unique")
    if not inconclusive and _tree_delta_captured(repo, branch, target):
        return RedundancyVerdict(
            redundant=True, forge_merged=forge_merged, unique_shas=unique_shas, source="synthetic-squash"
        )
    if not inconclusive and git.branch_merged(repo, branch, target):
        return RedundancyVerdict(
            redundant=True, forge_merged=forge_merged, unique_shas=unique_shas, source="branch-merged"
        )
    return RedundancyVerdict(
        redundant=False,
        forge_merged=forge_merged,
        unique_shas=unique_shas,
        source="inconclusive" if inconclusive else "not-redundant",
    )


def is_squash_merged(repo: str, branch: str, default: str) -> bool:
    """Whether ``branch``'s current tip is PROVABLY fully captured on ``origin/<default>``.

    The boolean view of :func:`branch_redundancy` the reaper and branch-prune
    pass share: ``True`` only when a content layer (cherry-zero / synthetic-squash
    / branch-merged) proved the tip redundant. The forge "merged" signal NEVER
    alone returns ``True`` — a forge-merged branch with unique current-tip content
    is kept for salvage, not deleted (the #2763 invariant). Survives a deleted
    local branch ref: the layers read the branch NAME, and the data-loss guards
    downstream keep an uncertain branch.
    """
    return branch_redundancy(repo, branch, f"origin/{default}").redundant


def _branch_captured_upstream(repo: str, branch: str, default: str) -> bool:
    """Whether every unique commit of ``branch`` is already in ``origin/<default>`` (patch-id).

    The forge-CLI-free per-commit cherry-zero signal the orphaned-stash reaper
    uses on a ``stash@{N}`` ref. ``git cherry`` prints ``- <sha>`` for each commit
    whose change is already upstream (a squash captured it) and ``+ <sha>`` for
    one that is not; the ref is captured only when cherry actually RAN, produced
    at least one comparison line, and every line is a ``-``.

    Two data-loss traps this closes (#F4.1). (1) The probe runs through the STRICT
    runner, so a real ``git cherry`` failure (unresolvable ``origin/<default>``,
    the ref gone, a corrupt repo) raises :class:`CommandFailedError` and is caught
    to ``False`` (not-captured) — the LENIENT runner degraded a failure to ``""``,
    which the ``all(...)`` below then read as vacuously-captured. (2) EMPTY cherry
    output is NOT captured: ``all([])`` is ``True``, but a stash ref that is a
    merge commit (``git cherry`` compares no patch and prints nothing) or any ref
    that produced no comparison line was never actually content-compared, so
    treating it as captured would drop the ONLY copy of the work. Both now resolve
    to ``False`` — a keep — so the orphaned-stash reaper keeps the stash on any
    inconclusive probe.

    The richer current-tip detector is :func:`branch_redundancy` (which also runs
    the synthetic-squash and ``--merged`` layers); this one stays the minimal
    per-commit form the stash path wants.
    """
    try:
        cherry = git.run_strict(repo=repo, args=["cherry", f"origin/{default}", branch])
    except CommandFailedError:
        return False
    lines = [line for line in cherry.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("-") for line in lines)
