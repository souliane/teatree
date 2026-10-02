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
import shutil
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from functools import cache, lru_cache
from tempfile import TemporaryDirectory
from typing import Any

from teatree.core.forge_pr_probe import find_open_pr_for_branch, forge_cli_env, forge_for_repo, gitlab_cli_env
from teatree.core.worktree.branch_landed import branch_content_landed_on_base
from teatree.core.worktree.branch_subject_prefilter import BranchCommit as _BranchCommit
from teatree.core.worktree.branch_subject_prefilter import SubjectPrefilterResult as _SubjectPrefilterResult
from teatree.core.worktree.branch_subject_prefilter import (
    prefilter_branch_commits_by_subject as _prefilter_branch_commits_by_subject,
)
from teatree.utils import git
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

BranchCommit = _BranchCommit
SubjectPrefilterResult = _SubjectPrefilterResult
prefilter_branch_commits_by_subject = _prefilter_branch_commits_by_subject

# The one RedundancyVerdict.source that means no content layer decided anything.
# Named so every consumer of the verdict's provenance reads the same token.
INCONCLUSIVE_SOURCE = "inconclusive"
# The source a landed verdict is DOWNGRADED to when the forge still has the branch's
# PR open (#3093). It sits on `branch_redundancy` itself, so no caller can reach a
# `redundant=True` that an open PR should have vetoed.
OPEN_PR_VETO_SOURCE = "open-pr-veto"


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
    ``content-landed`` / ``forge-merged-tip`` / ``not-redundant`` /
    ``inconclusive`` / ``open-pr-veto`` (a landed tip whose PR is still open).
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


_FALLBACK_DEFAULT_TARGET = "origin/main"


def effective_default_target(repo: str) -> str:
    """Resolve ``repo``'s REAL default branch as an ``origin/<default>`` ref.

    The content/redundancy probes must compare against the repo's ACTUAL default
    branch, not a hardcoded ``origin/main`` — a ``master``/``develop``-default
    repo measured against a base it does not have makes ``git cherry`` fail (or,
    worse, silently mis-measure). Shared here (a leaf both :mod:`cleanup` and
    :mod:`worktree_done` import) so the two teardown paths resolve the base the
    SAME way without an import cycle.

    A repo declared SINGLE-BRANCH (``single_branch_repos``) answers with its
    PINNED branch instead of the forge default. For a fork bootstrap the two are
    not the same thing: the repo default is still the empty initial commit while
    every change in the repo's life lands on the bootstrap branch behind one open
    PR. Measured against the default, EVERY branch reads as thousands of commits
    ahead and nothing is ever reapable — which is what left 31 worktrees standing
    over 37 branches on the two repos in that state, each one correctly kept for a
    reason that was an artefact of the wrong base.

    Fail-safe to ``origin/main`` on an unresolvable default: the downstream
    content gate fails CLOSED (an unresolvable target makes ``git cherry``
    inconclusive → a blocker → refuse), so a wrong/missing base keeps the branch
    rather than wiping it.
    """
    if pinned := _pinned_single_branch_target(repo):
        return f"origin/{pinned}"
    try:
        default = git.default_branch(repo)
    except (RuntimeError, CommandFailedError):
        return _FALLBACK_DEFAULT_TARGET
    return f"origin/{default}"


@lru_cache(maxsize=1)
def _declared_single_branch_repos() -> tuple[str, ...]:
    """The ``single_branch_repos`` entries, read ONCE per process.

    Cached because :func:`effective_default_target` is called per branch and per
    worktree by the reaper — a few hundred times in one ``workspace clean-all`` —
    and an uncached settings read there cost enough to push the dry-run past its
    own timeout. The declaration cannot change mid-run, and
    :func:`reset_single_branch_cache` exists for the tests that vary it.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return tuple(get_effective_settings().single_branch_repos)


def reset_single_branch_cache() -> None:
    """Drop the memoised declaration — for tests, and after a config write."""
    _declared_single_branch_repos.cache_clear()


def _pinned_single_branch_target(repo: str) -> str:
    """*repo*'s pinned branch when it is declared single-branch, else ``""``.

    Keyed on the REMOTE URL rather than the checkout path: the declaration names a
    repo slug, and a clone's directory name is whatever it was cloned into. The
    remote is only consulted once something is actually declared, so the default
    (nothing declared) costs no subprocess at all.

    Any failure to read the config or the remote answers ``""``, so the caller
    falls through to the forge default exactly as before — this must never be the
    reason a target cannot be resolved.
    """
    try:
        declared = _declared_single_branch_repos()
        if not declared:
            return ""
        from teatree.core.gates.single_branch_repo_guard import (  # noqa: PLC0415 — deferred: keeps this leaf light
            resolve_pinned_branch,
        )

        return resolve_pinned_branch(git.remote_url(repo), list(declared))
    except Exception:  # noqa: BLE001 — resolution is best-effort; fall through to the forge default.
        return ""


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


@cache
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
    forge = forge_for_repo(repo)
    if forge == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "mergeCommit", "--limit", "1"]
        return probe_host_cli(cmd, repo, lambda data: data[0]["mergeCommit"]["oid"])
    if forge == "gitlab":
        cmd = ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"]
        return probe_host_cli(cmd, repo, lambda data: data[0]["merge_commit_sha"])
    return ""


def probe_host_cli(cmd: list[str], repo: str, extract: Callable[[Any], str], *, timeout: float = 30.0) -> str:
    """Invoke a host CLI that may be missing, parse its JSON, extract the SHA.

    Swallows ``OSError`` (missing binary, permission denied in sandboxes) and
    JSON/key errors — both are legitimate "no merged PR found" outcomes.

    ``timeout`` bounds the host CLI invocation (seconds): a hung ``gh``/``glab``
    must not block ``clean-all`` or the loop tick. On expiry the
    ``subprocess.TimeoutExpired`` is swallowed and ``""`` is returned — the same
    fail-safe "not found / skip" value as every other failure path, so a timeout
    can never produce a positive merged signal and never wrongly reaps work.

    Authentication is mandatory: an anonymous private-repo read looks like
    "no PR" and could wrongly reap live work (souliane/teatree#4116).
    """
    tool = cmd[0] if cmd else ""
    env = forge_cli_env(repo) if tool == "gh" else gitlab_cli_env(repo) if tool == "glab" else None
    if env is None:
        return ""
    config_home = TemporaryDirectory(prefix="t3-glab-") if tool == "glab" else nullcontext("")
    with config_home as clean_config:
        if clean_config:
            env["GLAB_CONFIG_DIR"] = clean_config
        try:
            result = run_allowed_to_fail(cmd, cwd=repo, expected_codes=None, timeout=timeout, env=env)
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


def _forge_cli_available() -> bool:
    """Whether ANY forge CLI is on PATH — the reported-degradation signal.

    When neither ``gh`` nor ``glab`` exists, the forge rung of the landed ladder
    cannot run at all, and every branch it would have cleared is held. Callers
    report that degradation explicitly rather than letting the rung vanish
    silently (the ladder itself already fails CLOSED either way).
    """
    return shutil.which("gh") is not None or shutil.which("glab") is not None


def reset_forge_probe_cache() -> None:
    """Drop the per-run forge-probe memo — each cleanup pass starts fresh.

    The MERGED-record probes are memoized because one ``clean-all``
    interrogates the same (repo, branch) from several passes (redundancy, the
    #706 override, the emit record), each un-memoized probe is a network
    round-trip, and a merged record at a given tip is immutable once written.
    The open-PR probe is deliberately NOT cached: it reports mutable state that
    gates a PROTECT, and a stale "no open PR" would drop that protection. The
    memo is process-wide, so a long-lived loop process must reset it at every
    pass entry or it would keep answering from a previous tick.
    """
    _branch_pr_is_merged.cache_clear()
    _merged_pr_head_sha.cache_clear()
    _pr_merge_commit_sha.cache_clear()


@cache
def _merged_pr_head_sha(repo: str, branch: str) -> str:
    """The source-branch HEAD sha the forge recorded for ``branch``'s MERGED PR, or ``""``.

    The one landed instrument the squash-then-base-evolved case cannot defeat:
    a squash rewrites every SHA and later base edits erase the content from the
    base tree, but the forge still knows exactly which source tip it merged
    (GitHub ``headRefOid``, GitLab ``sha``). A merged record proves ONLY that
    recorded tip — the caller must compare it to the branch's CURRENT tip, so a
    branch that grew commits after the merge never reads as landed.

    Fail-safe to ``""``: a missing CLI, a network/auth failure, or an
    unparsable payload all answer "no record", never a positive sha.
    """
    forge = forge_for_repo(repo)
    if forge == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "headRefOid", "--limit", "1"]
        return probe_host_cli(cmd, repo, lambda data: data[0]["headRefOid"])
    if forge == "gitlab":
        cmd = ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"]
        return probe_host_cli(cmd, repo, lambda data: data[0]["sha"])
    return ""


def forge_merged_tip_captured(repo: str, branch: str) -> bool:
    """Whether the forge merged ``branch`` at EXACTLY its current local tip.

    The forge rung of the landed ladder: authoritative for a merged branch the
    base has since evolved past (no git-local layer can see that one), and
    fail-CLOSED everywhere else — no record, an unreadable local tip, or a tip
    that moved after the merge all read NOT captured.
    """
    recorded = _merged_pr_head_sha(repo, branch)
    if not recorded:
        return False
    tip = git.run(repo=repo, args=["rev-parse", f"{branch}^{{commit}}"])
    return bool(tip) and tip == recorded


@cache
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
    forge = forge_for_repo(repo)
    if forge == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"]
        return bool(probe_host_cli(cmd, repo, lambda data: str(data[0]["number"])))
    if forge == "gitlab":
        cmd = ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"]
        return bool(probe_host_cli(cmd, repo, lambda data: str(data[0]["iid"])))
    return False


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
    return find_open_pr_for_branch(repo, branch).is_found


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
    """The canonical verdict: is ``branch``'s CURRENT tip provably on ``target`` AND done?

    A landed verdict is vetoed while the forge reports an OPEN PR (#3093): a content
    layer can prove a live PR's tip captured, and deleting under one destroys work
    nobody merged. It sits HERE because four callers read the verdict directly, and is
    fail-safe — an unreachable forge skips the veto, never manufactures one.

    Four CONTENT layers decide ``redundant`` (see the module docstring):
    cherry-zero (:func:`content_equivalence_blockers` empty), synthetic-squash
    (:func:`_tree_delta_captured`), ``git branch --merged``, then the
    path-independent blob probe (:func:`branch_content_landed_on_base` — the
    refactor case, where the base took the branch's exact bytes at another
    path). Above them sits the one non-git rung: a forge merge record at
    EXACTLY the current tip (:func:`forge_merged_tip_captured`) — the only
    instrument that survives a squash whose content the base later evolved
    past. A bare forge "merged" signal still NEVER decides anything — a
    forge-merged tip that moved after the merge is returned NOT-redundant with
    its content in ``unique_shas`` and surfaced via
    :attr:`RedundancyVerdict.merged_with_post_merge_work`.

    Fail-CLOSED on an inconclusive content probe: an erroring ``git cherry``
    (``content_equivalence_blockers`` returns a parenthesised marker) skips the
    squash/merged layers and returns NOT-redundant, so an uncertain branch is
    kept, never deleted.
    """
    verdict = _content_redundancy(repo, branch, target)
    if not verdict.redundant or not _branch_has_open_pr(repo, branch):
        return verdict
    return replace(verdict, redundant=False, source=OPEN_PR_VETO_SOURCE)


def _content_redundancy(repo: str, branch: str, target: str) -> RedundancyVerdict:
    """The layered CONTENT verdict, before the open-PR veto :func:`branch_redundancy` applies."""
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
    if not inconclusive and branch_content_landed_on_base(repo, branch, target):
        return RedundancyVerdict(
            redundant=True, forge_merged=forge_merged, unique_shas=unique_shas, source="content-landed"
        )
    # Deliberately NOT gated on the git-local probes: the forge record is what
    # survives a squash whose content the base has since evolved past.
    if forge_merged and forge_merged_tip_captured(repo, branch):
        return RedundancyVerdict(
            redundant=True, forge_merged=forge_merged, unique_shas=unique_shas, source="forge-merged-tip"
        )
    return RedundancyVerdict(
        redundant=False,
        forge_merged=forge_merged,
        unique_shas=unique_shas,
        source=INCONCLUSIVE_SOURCE if inconclusive else "not-redundant",
    )


def is_squash_merged(repo: str, branch: str, default: str) -> bool:
    """Whether ``branch``'s current tip is PROVABLY fully captured on ``origin/<default>``.

    The boolean view of :func:`branch_redundancy` the reaper and branch-prune pass
    share — every rung, the #2763 forge-alone invariant and the open-PR veto are its,
    not this wrapper's. Survives a deleted local branch ref: the layers read the branch
    NAME, and the data-loss guards downstream keep an uncertain branch.
    """
    return branch_redundancy(repo, branch, f"origin/{default}").redundant
