"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands.

The classifier below is the reason this module can be honest about squash-merges:
``git log <branch> --not origin/main`` detects commits by SHA, but a squash-merge
creates a new SHA on the default branch. Without subject-matching, every
squash-merged branch looks "unsynced" and blocks cleanup. Comparing against
``origin/main`` (not ``--remotes``) is essential — ``--remotes`` would also
exclude the feature branch's own remote tracking ref, hiding commits that are
pushed but not yet on main.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

from teatree.config import load_config
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree_env import worktree_pg_connection
from teatree.core.worktree_recovery import _has_unpushed_commits, capture_recovery_artifact
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.postgres_secret import remove_postgres_pass_entry
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


_PR_SUFFIX_RE = re.compile(r"(?:\s*\(#\d+\))+$")
_RELEASE_NOTE_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*\([^)]+\)\s*$")
_TYPE_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!?:\s*", re.IGNORECASE)
_BRANCH_LOG_FIELDS = 3
_SUBJECT_PREVIEW_LIMIT = 3


@dataclass(slots=True)
class CleanupResult:
    """Outcome of a single :func:`cleanup_worktree` teardown.

    ``label`` is the human-readable summary (still printed by the
    interactive ``clean-all`` / ``clean-merged`` callers and surfaced as
    the runner ``detail``). ``errors`` is the structured, machine-readable
    channel: every teardown step that failed appends a descriptive string
    here instead of crashing mid-teardown or being swallowed by a
    ``suppress(Exception)`` (#877).

    #932's lesson — a swallowed string the caller never inspects is not
    surfacing. Sync backends push ``errors`` into ``SyncResult.errors`` and
    runners fold it into their failure detail, so a teardown failure
    actually reaches the operator/exit path.
    """

    label: str
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when every teardown step succeeded."""
        return not self.errors

    def __str__(self) -> str:
        if self.errors:
            return f"{self.label} [with errors: {'; '.join(self.errors)}]"
        return self.label


@dataclass(frozen=True)
class BranchCommit:
    """A commit on a branch that is not reachable from any remote by SHA."""

    sha: str
    subject: str
    is_merge: bool


@dataclass(frozen=True)
class BranchClassification:
    """Structured view of a branch's unsynced commits, split by disposition.

    ``squash_merged`` — subject matches a commit on the target branch, so the
    content is already integrated (typical squash-merge case, including the
    ``relax:`` → ``feat:`` prefix rewrite).

    ``merge_commits`` — commits with multiple parents (Merge branch 'main' into
    feature). They carry no net content of their own and are safe to discard.

    ``genuinely_ahead`` — everything else. The branch has work that does not
    appear on the target, so removing it would lose content.
    """

    squash_merged: list[BranchCommit] = field(default_factory=list)
    merge_commits: list[BranchCommit] = field(default_factory=list)
    genuinely_ahead: list[BranchCommit] = field(default_factory=list)


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


def classify_branch_commits(repo: str, branch: str, target: str = "origin/main") -> BranchClassification:
    """Split the branch's unsynced commits into squash-merged / merge / genuinely-ahead buckets.

    Runs two git log invocations: one to list branch commits not on any remote
    (same as :func:`git.unsynced_commits`), one to fetch subjects on ``target``
    for subject matching.
    """
    raw = git.run(
        repo=repo,
        args=["log", branch, "--not", target, "--format=%H%x00%P%x00%s"],
    )
    classification = BranchClassification()
    if not raw.strip():
        return classification

    target_raw = git.run(repo=repo, args=["log", target, "--format=%s", "-n", "500"])
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


def _raise_if_genuinely_ahead(repo_main: str, worktree: Worktree) -> None:
    """Raise ``RuntimeError`` when the branch carries commits not on ``origin/main``.

    Merge commits and squash-merged commits are ignored — only ``genuinely_ahead``
    work blocks cleanup. Two fallbacks run before refusing, both confirming the
    work already shipped: first the PR's merge commit tree is compared against the
    branch tip (an empty diff means the cumulative content is captured in the
    squash, typical for post-merge retro commits); then, for branches that
    diverged so far the squash tree no longer matches, the forge is asked
    canonically whether the branch's PR is merged (#1578). The error message
    lists up to ``_SUBJECT_PREVIEW_LIMIT`` commit subjects so the caller can
    decide whether to push or abandon.
    """
    unsynced = git.unsynced_commits(repo_main, worktree.branch)
    if not unsynced:
        return
    classification = classify_branch_commits(repo_main, worktree.branch)
    if not classification.genuinely_ahead:
        return
    if _branch_tree_matches_squash(repo_main, worktree.branch):
        return
    if _branch_pr_is_merged(repo_main, worktree.branch):
        return
    preview = classification.genuinely_ahead[:_SUBJECT_PREVIEW_LIMIT]
    subjects = ", ".join(c.subject for c in preview)
    if len(classification.genuinely_ahead) > _SUBJECT_PREVIEW_LIMIT:
        subjects += ", …"
    msg = (
        f"{worktree.repo_path} ({worktree.branch}): "
        f"refused cleanup — {len(classification.genuinely_ahead)} unsynced commit(s) "
        f"not on origin/main: {subjects}. "
        "Push them to a new branch or pass force=True."
    )
    raise RuntimeError(msg)


def _raise_if_unpushed(repo_main: str, worktree: Worktree) -> None:
    """Raise ``RuntimeError`` when the branch has commits on NO remote ref (#706).

    The data-loss guard. The lifecycle FSM can read a teardown-eligible state
    (MERGED / shipped) while the branch was never actually pushed — async ship
    never drained (#707/#708). Removing the git worktree then destroys those
    commits irrecoverably once refs are pruned or ``git gc`` runs.

    This is intentionally distinct from :func:`_raise_if_genuinely_ahead`
    (squash-merge-aware, ``origin/main``-relative cleanup hygiene). A branch
    pushed to its own remote tracking ref but not yet merged to main is SAFE
    here — the work survives on the remote. Only commits absent from every
    ``refs/remotes/*`` block teardown. The error names the branch, the count,
    and up to ``_SUBJECT_PREVIEW_LIMIT`` short SHAs so the loss is loud.

    **Fails closed.** If the probe itself errors (invalid/missing branch,
    corrupt repo, any ``git log`` failure) it raises ``CommandFailedError``;
    we translate that into a refusal rather than proceeding, because an
    inconclusive probe means we cannot prove the commits are pushed.

    **Canonical merged override (#1578).** A squash-merge creates a new SHA on
    the default branch and deletes the source ref, so the branch's own commits
    are absent from every remote even though the work shipped. Before refusing,
    the forge is asked whether the branch's PR is merged; a positive answer is
    the ground truth that the content is safe on the default branch, so teardown
    proceeds. The check fails safe to skip — only a positive merged signal
    overrides; any uncertainty keeps the refusal.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(repo_main, worktree.branch)
    except CommandFailedError as exc:
        msg = (
            f"{worktree.repo_path} ({worktree.branch}): "
            f"refused teardown — could not verify the branch is pushed "
            f"(git probe failed: {exc}). Push the branch or pass force=True to discard."
        )
        raise RuntimeError(msg) from exc
    if not unpushed:
        return
    if _branch_pr_is_merged(repo_main, worktree.branch):
        return
    preview = unpushed[:_SUBJECT_PREVIEW_LIMIT]
    shas = ", ".join(preview)
    if len(unpushed) > _SUBJECT_PREVIEW_LIMIT:
        shas += ", …"
    msg = (
        f"{worktree.repo_path} ({worktree.branch}): "
        f"refused teardown — {len(unpushed)} commit(s) on NO remote (data loss): "
        f"{shas}. Push the branch or pass force=True to discard."
    )
    raise RuntimeError(msg)


def _resolve_worktree_path(workspace: Path, worktree: Worktree) -> str:
    """Return the on-disk worktree path, preferring extras and falling back to the canonical layout.

    Provisioning records ``worktree_path`` in ``Worktree.extra`` after a
    successful ``git worktree add``. When that record is missing (extras lost,
    row created before the path was set, manual provisioning), derive the path
    from the canonical layout used by ``WorktreeProvisioner._create``:
    ``workspace/<branch>/<repo-leaf>``.
    """
    stored = (worktree.extra or {}).get("worktree_path", "")
    if stored:
        return stored
    return str(workspace / worktree.branch / Path(worktree.repo_path).name)


def _remove_git_worktree(
    repo_main: Path,
    wt_path: str,
    worktree: Worktree,
    *,
    force: bool,
    strict_hygiene: bool,
) -> list[str]:
    """Remove the git worktree + branch from the source repo, returning any error messages.

    Returns an empty list on success. The source repo missing, the git worktree
    remove failing, or the branch delete failing each yield one entry. Failures
    are surfaced (not raised) so unrelated cleanup steps still run.
    """
    if not repo_main.is_dir():
        return [f"source repo missing at {repo_main}"]
    if not force:
        # #706 — the data-loss guard runs first. It is the seam every
        # Worktree-row-driven teardown caller funnels through (execute_teardown
        # / WorktreeTeardown, WorktreeTeardownRunner, clean-merged, clean-all,
        # sync-backend merge cleanup, abandon) and blocks removal of commits
        # that exist on no remote at all (the bug that destroyed worktrees).
        # It is never skipped except by an explicit force override.
        #
        # One narrower path is NOT routed here: _workspace_cleanup.
        # prune_squash_merged() deletes a branch+worktree directly via
        # git.worktree_remove/branch_delete, but only AFTER is_squash_merged()
        # has confirmed the content is on a remote (merged PR or empty diff vs
        # origin/<default>), so it is low risk. Routing it through this guard
        # would require synthesising a Worktree row and would risk false-
        # blocking legitimately squash-merged branches whose local SHAs differ
        # from the squash commit. Unifying that path is tracked as follow-up
        # (see #706 review) rather than forced here.
        _raise_if_unpushed(str(repo_main), worktree)
        # The squash-merge-aware origin/main hygiene gate is stricter: it also
        # blocks pushed-but-unmerged branches. Sync backends and interactive
        # clean-all want it (they clean only on detected merge / orphan reap);
        # the automated FSM teardown path does not (the ticket is MERGED and
        # the work is already preserved on the remote).
        if strict_hygiene:
            _raise_if_genuinely_ahead(str(repo_main), worktree)
    errors: list[str] = []
    # #835 — capture before the destructive remove. When force=True the guards
    # above are skipped (the clean-all / abandon reaping path that destroyed a
    # completed-but-uncommitted change set): a dirty or unpushed worktree gets a
    # restorable bundle + working-tree diff under the system temp dir first. A
    # clean, fully-pushed worktree captures nothing — the hard-delete path is
    # unchanged.
    try:
        capture_recovery_artifact(repo_main, wt_path, worktree)
    except Exception as exc:
        # #1506 — under force the recovery artifact is the ONLY protection, so a
        # capture failure must not silently fall through to the destructive
        # remove. Re-check (with the fail-closed probe) whether this worktree
        # actually had work to lose; if so, refuse the teardown for it just like
        # the non-force #706 guard does — raise before the destructive
        # remove + the ``worktree.delete()`` DB-row drop, so the worktree is
        # left intact on disk AND still tracked (no orphaned-on-disk row). #835's
        # non-blocking intent is preserved for the safe case: a clean +
        # fully-pushed worktree (where the failed capture was a no-op anyway)
        # falls through and is still reaped.
        logger.exception("recovery capture failed for %s (%s)", worktree.repo_path, worktree.branch)
        if _worktree_has_work_to_lose(repo_main, wt_path, worktree):
            msg = (
                f"{worktree.repo_path} ({worktree.branch}): "
                f"refused teardown — recovery capture failed ({exc}) and the worktree has "
                f"unrecoverable work (dirty or unpushed). Kept it on disk at {wt_path}; "
                f"restore or push it, then re-run cleanup."
            )
            raise RuntimeError(msg) from exc
        errors.append(f"recovery capture failed for {worktree.branch}: {exc}")
    if not git.worktree_remove(str(repo_main), wt_path):
        errors.append(f"git worktree remove failed for {wt_path}")
    if not git.branch_delete(str(repo_main), worktree.branch):
        errors.append(f"git branch -D failed for {worktree.branch}")
    return errors


def _worktree_has_work_to_lose(repo_main: Path, wt_path: str, worktree: Worktree) -> bool:
    """Whether removing this worktree would destroy unrecoverable work.

    Re-evaluates the same dirty/unpushed criteria :func:`capture_recovery_artifact`
    uses, but **fails closed** at every step: this guards an irreversible
    ``branch -D`` + ``worktree remove`` after the recovery capture already
    failed, so "couldn't determine" must mean "might lose work", not "safe".

    Unpushed commits are checked first via the same fail-open probe the capture
    uses (it returns ``True`` on an inconclusive ``git log``). Those commits
    live in the main clone's object store, so a missing worktree dir does not
    make them safe — the branch is the only copy.

    The dirty working-tree check runs only when the dir is present and uses the
    strict porcelain probe; an inconclusive ``git status`` (lock contention,
    corrupt index) raises and is treated as "might be dirty".

    Returns ``False`` only when both checks positively confirm there is nothing
    to lose — a clean (or already-gone) worktree whose branch is fully pushed,
    the safe case #835's non-blocking intent still reaps.
    """
    if _has_unpushed_commits(repo_main, worktree.branch):
        return True
    if not Path(wt_path).is_dir():
        return False
    try:
        return bool(git.status_porcelain_strict(wt_path))
    except CommandFailedError:
        return True


def _reap_external_resources(overlay: "OverlayBase", worktree: Worktree, step_errors: list[str]) -> str:
    """Run the overlay's external-resource reaper, returning a label suffix.

    Appends a descriptive string to *step_errors* on failure (collect-and-
    surface, never crash mid-teardown) and returns the joined outcomes as a
    ``" — …"`` suffix for the cleanup label, or ``""`` when nothing was removed
    or the reaper failed.
    """
    try:
        reaped = overlay.reap_worktree_external_resources(worktree)
    except Exception as exc:
        logger.exception("external-resource reap failed for %s (%s)", worktree.repo_path, worktree.branch)
        step_errors.append(f"external-resource reap failed for {worktree.branch}: {exc}")
        return ""
    return " — " + "; ".join(reaped) if reaped else ""


def cleanup_worktree(worktree: Worktree, *, force: bool = False, strict_hygiene: bool = True) -> CleanupResult:
    """Remove a single worktree: git worktree, branch, DB, overlay cleanup.

    Deletes the Worktree record from the database and returns a
    :class:`CleanupResult`. Individual teardown-step failures (overlay
    hook, git worktree/branch removal, DB drop, pass-entry removal,
    recovery capture) are captured into ``result.errors`` and the
    remaining steps still run — collect-and-surface, never crash
    mid-teardown leaving other resources orphaned (#877). The caller is
    responsible for routing ``result.errors`` to its visible channel
    (``SyncResult.errors`` for sync backends, runner detail for runners).

    Two guards protect against losing work, both bypassed only by an explicit
    ``force=True``.

    Data-loss guard (#706, always on): raises ``RuntimeError`` when the branch
    has commits on NO remote ref — removing the worktree would destroy them
    irrecoverably.

    Hygiene gate (``strict_hygiene``, default on): additionally raises when the
    branch is genuinely ahead of ``origin/main`` and not squash-merged. Sync
    backends and interactive ``clean-all`` keep this on; the automated FSM
    teardown path passes ``strict_hygiene=False`` (the ticket is MERGED and the
    branch is already on its remote).

    Recovery-capture backstop (#1506, ``force=True`` only): when the #706/#835
    guards are bypassed by force, the recovery capture is the only protection.
    If that capture *fails* and the worktree still has work to lose (dirty or
    unpushed, determined fail-closed), this raises ``RuntimeError`` too — before
    the destructive remove and the DB-row delete — so the worktree is left
    intact and tracked rather than silently destroyed. A proven clean+pushed
    worktree whose (no-op) capture failed is still reaped, with the failure in
    ``result.errors``.

    Pass ``force=True`` only from trusted callers (explicit operator override,
    tests, programmatic API).
    """
    workspace = load_config().user.workspace_dir
    wt_path = _resolve_worktree_path(workspace, worktree)
    overlay = get_overlay()

    if Path(wt_path).is_dir() and git.status_porcelain(wt_path):
        logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)

    # Stop the docker compose project FIRST so containers don't leak when
    # this path is reached outside the WorktreeTeardownRunner (#1306) —
    # the auto-merged-ticket teardown, `clean-merged`, `clean-all`, and
    # sync backends all funnel through here. Idempotent: docker compose
    # down on a project with no containers is a no-op.
    from teatree.core.runners.worktree_start import compose_project, docker_compose_down  # noqa: PLC0415

    docker_compose_down(compose_project(worktree))

    step_errors: list[str] = []
    for step in overlay.get_cleanup_steps(worktree):
        try:
            step.callable()
        except Exception as exc:
            logger.exception("cleanup step failed for %s: %s", worktree.repo_path, step.description)
            step_errors.append(f"{step.description}: {exc}")

    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    step_errors.extend(_remove_git_worktree(repo_main, wt_path, worktree, force=force, strict_hygiene=strict_hygiene))

    if worktree.db_name:
        db_user, _, _ = worktree_pg_connection(worktree, overlay=overlay)
        try:
            drop_db(worktree.db_name, user=db_user)
        except Exception as exc:
            logger.exception("dropdb failed for %s (%s)", worktree.db_name, worktree.repo_path)
            step_errors.append(f"dropdb failed for {worktree.db_name}: {exc}")

    if getattr(overlay.config, "teardown_removes_pass_entries", False) is True:
        ticket = worktree.ticket
        if ticket is not None:
            try:
                remove_postgres_pass_entry(ticket.ticket_number)
            except Exception as exc:
                logger.exception("pass-entry removal failed for %s", worktree.repo_path)
                step_errors.append(f"pass-entry removal failed for {ticket.ticket_number}: {exc}")

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    label += _reap_external_resources(overlay, worktree, step_errors)

    ticket_id = worktree.ticket.pk
    worktree.delete()
    if not Worktree.objects.filter(ticket_id=ticket_id).exists():
        Ticket.objects.get(pk=ticket_id).release_redis_slot()
    return CleanupResult(label=label, errors=step_errors)
