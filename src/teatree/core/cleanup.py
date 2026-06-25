"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands.

The squash-merge-aware classification this module relies on lives in
:mod:`teatree.core.branch_classification`; the data-loss guards and the
worktree-teardown orchestration live here. The names re-exported below keep the
import surface (``from teatree.core.cleanup import classify_branch_commits``,
``cleanup_mod._branch_pr_is_merged``) stable for the management commands and the
sync backends that funnel through this seam.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

from django.core.exceptions import ImproperlyConfigured

from teatree.config import load_config
from teatree.core import prek_hook
from teatree.core._overlay_teardown import reap_external_resources, run_overlay_cleanup_steps
from teatree.core.branch_classification import (
    BranchClassification,
    BranchCommit,
    _branch_pr_is_merged,
    _branch_tree_matches_squash,
    _pr_merge_commit_sha,
    classify_branch_commits,
    content_equivalence_blockers,
    probe_host_cli,
)
from teatree.core.cleanup_orphan_ref import raise_or_reap_orphan_ref
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Worktree
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.worktree_env import compose_project, worktree_pg_connection
from teatree.core.worktree_recovery import _has_unpushed_commits, capture_recovery_artifact
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.postgres_secret import remove_postgres_pass_entry
from teatree.utils.run import CommandFailedError

__all__ = [
    "BranchClassification",
    "BranchCommit",
    "CleanupResult",
    "_branch_pr_is_merged",
    "_branch_tree_matches_squash",
    "_pr_merge_commit_sha",
    "classify_branch_commits",
    "cleanup_worktree",
    "probe_host_cli",
]

logger = logging.getLogger(__name__)


_SUBJECT_PREVIEW_LIMIT = 3

# A full git sha is 40 hex chars; below this a "sha" is a fail-closed diagnostic
# string (e.g. "(git cherry failed …)") surfaced verbatim, not sliced.
_SHORT_SHA_LEN = 7


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
class _EffectiveTarget:
    """The worktree's ACTUAL teardown target, resolved from git — not the DB row.

    ``Worktree.branch`` (the DB-recorded slug) can drift from the branch actually
    checked out in the on-disk worktree: provisioning records the ticket slug
    while a later checkout/rename inside the worktree leaves the slug ref pointing
    elsewhere (or gone). Trusting the slug makes the data-loss probe interrogate
    the wrong branch (missing the real unpushed work, or erroring with "unknown
    revision") and makes ``branch -D`` no-op on a non-existent slug, leaving the
    REAL branch dangling after its worktree is removed.

    This resolves the truth at the teardown seam:

    - ``ref`` is the revision to probe for unpushed work: the literal ``HEAD``
        in the worktree dir when present (robust to drift AND detached HEAD), or
        the DB slug in the main clone when the dir is gone.
    - ``probe_repo`` is where to run the probe: the worktree dir when present
        (``HEAD`` is meaningful there), else the main clone.
    - ``branch_to_delete`` is the named branch to ``branch -D``: the real
        checked-out branch when present and named, the DB slug as fallback, or
        ``None`` when detached (no branch to delete).
    - ``label`` is the branch/ref shown in refusal messages and forge probes.
    """

    ref: str
    probe_repo: str
    branch_to_delete: str | None
    label: str


def _effective_target(repo_main: str, wt_path: str, worktree: Worktree) -> _EffectiveTarget:
    """Resolve the worktree's actual teardown target from git, falling back to the DB row.

    When ``wt_path`` is a present git worktree, the effective branch is
    ``git -C <wt_path> rev-parse --abbrev-ref HEAD`` (``DETACHED_HEAD`` when
    detached); the probe runs against ``HEAD`` in the worktree dir, which exactly
    reflects what removal would orphan and is immune to a drifted DB slug. When
    the dir is gone (worktree already removed, DB row lingering) — or the
    branch read came back empty (a broken worktree git could not resolve) — the
    only handle left is the DB ``Worktree.branch`` slug, probed in the main clone
    — the pre-#706/#835 behaviour, preserved as the fallback.
    """
    effective = git.current_branch(wt_path) if Path(wt_path).is_dir() else ""
    if effective:
        detached = effective == git.DETACHED_HEAD
        return _EffectiveTarget(
            ref=git.DETACHED_HEAD,
            probe_repo=wt_path,
            branch_to_delete=None if detached else effective,
            label=effective,
        )
    return _EffectiveTarget(
        ref=worktree.branch,
        probe_repo=repo_main,
        branch_to_delete=worktree.branch,
        label=worktree.branch,
    )


def _raise_if_genuinely_ahead(repo_main: str, worktree: Worktree, target: _EffectiveTarget) -> None:
    """Raise ``RuntimeError`` when the branch carries work not provably on ``origin/main``.

    Operates on ``target`` (the effective branch resolved from git), not the
    possibly-drifted DB ``Worktree.branch`` slug. The ``origin/main``-relative
    check needs a named branch reachable from the main clone, so it runs against
    the main clone using the effective label; a detached HEAD has no named branch
    and is skipped (the #706 data-loss guard already covered its unpushed commits
    via the worktree-dir probe).

    **Content-equivalence authorizes the destroy, not subject-match (#2609).**
    :func:`classify_branch_commits` is a cheap SUBJECT recognizer — it cannot
    authorize a force-delete, because a genuine un-upstreamed commit whose subject
    collides with an already-upstreamed subject (a routine ``docs: update skills``)
    drains into ``squash_merged`` and would falsely empty ``genuinely_ahead``. The
    AUTHORITATIVE gate is :func:`content_equivalence_blockers` (``git cherry``
    patch-id + a merge-commit check): an empty blocker list is positive proof every
    commit's CONTENT is already upstream, so the worktree is safe to remove. It
    fails CLOSED — any inconclusive git probe reports a blocker and the cleanup is
    refused.

    Two positive merged-evidence fallbacks override a non-empty blocker list, both
    confirming the work already shipped despite a patch-id mismatch: the PR's merge
    commit tree compared against the branch tip (an empty diff means the cumulative
    content is captured in the squash, typical for post-merge retro commits), and
    the forge's canonical PR-merged report (#1578, for branches diverged so far the
    squash tree no longer matches). The error message lists up to
    ``_SUBJECT_PREVIEW_LIMIT`` blockers so the caller can decide whether to push or
    abandon.
    """
    branch = target.branch_to_delete
    if branch is None:
        # Detached HEAD: no named branch to classify against origin/main. The
        # #706 unpushed guard already probed HEAD in the worktree dir, so any
        # work-to-lose was caught there.
        return
    if not git.unsynced_commits(repo_main, branch):
        return
    blockers = content_equivalence_blockers(repo_main, branch)
    if not blockers:
        return
    if _branch_tree_matches_squash(repo_main, branch):
        return
    if _branch_pr_is_merged(repo_main, branch):
        return
    preview = blockers[:_SUBJECT_PREVIEW_LIMIT]
    listed = ", ".join(sha[:_SHORT_SHA_LEN] if len(sha) >= _SHORT_SHA_LEN else sha for sha in preview)
    if len(blockers) > _SUBJECT_PREVIEW_LIMIT:
        listed += ", …"
    msg = (
        f"{worktree.repo_path} ({target.label}): "
        f"refused cleanup — {len(blockers)} commit(s) not provably on origin/main "
        f"(content not upstream): {listed}. "
        "Push them to a new branch or pass force=True."
    )
    raise RuntimeError(msg)


def _remote_tracking_ref_exists(repo: str, branch: str) -> bool:
    """Whether ``refs/remotes/origin/<branch>`` is present in ``repo``.

    A forge squash-merge deletes the source ref remotely but leaves a STALE local
    tracking ref until ``fetch --prune`` removes it, so sampling this BEFORE the
    fetch is the forge-CLI-free proof the branch was once pushed (#2205).
    """
    return git.check(repo=repo, args=["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])


def _ref_tree_matches_default(repo: str, ref: str) -> bool:
    """Return ``True`` when ``ref``'s tree is identical to ``origin/<default>``.

    A squash-merge copies the source tree verbatim onto the default branch, so
    ``git diff --quiet ref origin/<default>`` exits 0 when the content is already
    there. This is a NECESSARY but NOT SUFFICIENT signal for "safe to delete":
    a never-pushed local-only branch whose work was fully reverted in a later
    commit has the same matching tree yet exists on no remote, so deleting it
    would destroy the only copy. Tree equality must therefore be confirmed only
    in conjunction with positive merged-evidence — see
    :func:`_ref_captured_by_merge`.

    **Fails open.** Any error (invalid ``ref``, missing ``origin/<default>``,
    corrupt repo) returns ``False`` so the merged-evidence gate above it keeps
    the fail-closed posture of the data-loss guard.
    """
    try:
        remote_default = f"origin/{git.default_branch(repo)}"
    except RuntimeError:
        return False
    return git.check(repo=repo, args=["diff", "--quiet", ref, remote_default])


def _ref_captured_by_merge(repo: str, ref: str, branch: str | None, *, remote_ref_was_present: bool) -> bool:
    """Whether ``ref`` is safe to delete because its work was MERGED (#2205, data-loss fix).

    After a squash-merge the source branch's commits are absent from every
    remote (the squash is a new SHA; the source ref was deleted), so
    ``commits_absent_from_all_remotes`` reports the branch as "on NO remote"
    even though the content shipped. Distinguishing that safe case from a
    genuinely local-only branch (never pushed anywhere) requires POSITIVE
    evidence the work was integrated — tree equality alone is a false positive,
    because a fully-reverted local branch has the same matching tree.

    Two positive merged signals are accepted, either of which AND a matching tree
    permits deletion. ``remote_ref_was_present`` means the local
    ``refs/remotes/origin/<branch>`` tracking ref existed before this run's
    fetch/prune: a forge squash-merge deletes the source ref remotely but leaves a
    stale tracking ref locally until the prune, so its prior presence proves the
    branch was once pushed (the squash-merge scenario), which a never-pushed branch
    never produces. :func:`_branch_pr_is_merged` is the forge's canonical report
    that the branch's PR/MR merged.

    With NO merged-evidence the branch is kept regardless of tree equality: a
    local-only branch whose tree matches the default branch must never be deleted.
    """
    if not (remote_ref_was_present or (branch is not None and _branch_pr_is_merged(repo, branch))):
        return False
    return _ref_tree_matches_default(repo, ref)


def _raise_if_unpushed(repo_main: str, worktree: Worktree, target: _EffectiveTarget) -> None:
    """Raise ``RuntimeError`` when the worktree's tip has commits on NO remote ref (#706).

    The data-loss guard. The lifecycle FSM can read a teardown-eligible state
    (MERGED / shipped) while the branch was never actually pushed — async ship
    never drained (#707/#708). Removing the git worktree then destroys those
    commits irrecoverably once refs are pruned or ``git gc`` runs.

    Probes ``target`` (the effective branch/HEAD resolved from git), not the
    possibly-drifted DB ``Worktree.branch`` slug. When the worktree dir is
    present it probes ``HEAD`` in the worktree dir itself — robust to DB drift
    AND detached HEAD, and reflecting exactly what removal would orphan; when the
    dir is gone it falls back to probing the slug in the main clone.

    This is intentionally distinct from :func:`_raise_if_genuinely_ahead`
    (squash-merge-aware, ``origin/main``-relative cleanup hygiene). A branch
    pushed to its own remote tracking ref but not yet merged to main is SAFE
    here — the work survives on the remote. Only commits absent from every
    ``refs/remotes/*`` block teardown. The error names the branch, the count,
    and up to ``_SUBJECT_PREVIEW_LIMIT`` short SHAs so the loss is loud.

    **Fails closed, with a branch-ref-gone reap path.** If the probe errors
    (``CommandFailedError``) the named ref is gone — a forge post-merge branch
    deletion leaves the worktree's HEAD a dangling symref, so ``git log HEAD
    --not --remotes`` exits 128. Branch-ref-gone is itself the post-merge-delete
    signal: before refusing, :func:`raise_or_reap_orphan_ref` (via
    :func:`classify_orphan_ref`) recovers the worktree's last HEAD SHA from its
    per-worktree reflog and reaps only when that SHA is POSITIVELY contained in a
    remote. HEAD on no remote (genuinely-unsynced
    local work), an unrecoverable HEAD, or any error keeps the refusal — the probe
    failure never reaps on uncertainty, only on positive proof the work shipped.

    **Merged override (#1578 / #2205).** A squash-merge creates a new SHA on the
    default branch and deletes the source ref, so the branch's own commits are
    absent from every remote even though the work shipped. Before refusing, two
    positive merged signals are consulted via :func:`_ref_captured_by_merge`: the
    forge is asked (by the named branch) whether its PR is merged, and the local
    ``origin/<branch>`` tracking ref's presence is sampled BEFORE the fetch (a
    forge squash-merge leaves a stale tracking ref locally until the fetch prunes
    it). Either signal AND a matching tree lets teardown proceed.

    **Data-loss safety (#2205).** Tree equality is NEVER a standalone override: a
    never-pushed local-only branch whose work was reverted has the same matching
    tree, so without one of the positive merged signals the branch is kept. The
    check fails safe to skip — only positive merged-evidence overrides; any
    uncertainty keeps the refusal.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(target.probe_repo, target.ref)
    except CommandFailedError as exc:
        raise_or_reap_orphan_ref(worktree, target, exc)
        return
    if not unpushed:
        return
    # Sample the stale tracking ref BEFORE the fetch prunes it; fetch after so the
    # tree comparison reflects the current remote, not a stale local mirror.
    remote_ref_was_present = target.branch_to_delete is not None and _remote_tracking_ref_exists(
        repo_main, target.branch_to_delete
    )
    git.fetch(repo_main, "origin")
    if _ref_captured_by_merge(
        target.probe_repo,
        target.ref,
        target.branch_to_delete,
        remote_ref_was_present=remote_ref_was_present,
    ):
        return
    preview = unpushed[:_SUBJECT_PREVIEW_LIMIT]
    shas = ", ".join(preview)
    if len(unpushed) > _SUBJECT_PREVIEW_LIMIT:
        shas += ", …"
    msg = (
        f"{worktree.repo_path} ({target.label}): "
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
    # Resolve the teardown target from git ONCE, then operate on it throughout.
    # ``Worktree.branch`` (the DB slug) can drift from the branch actually
    # checked out in the worktree; trusting it makes the data-loss probe
    # interrogate the wrong branch and makes ``branch -D`` no-op on a phantom
    # slug, leaving the real branch dangling after its worktree is removed.
    target = _effective_target(str(repo_main), wt_path, worktree)
    if not force:
        # #706 — the data-loss guard runs first. It is the seam every
        # Worktree-row-driven teardown caller funnels through (execute_teardown
        # / WorktreeTeardown, WorktreeTeardownRunner, clean-merged, clean-all,
        # sync-backend merge cleanup, abandon) and blocks removal of commits
        # that exist on no remote at all (the bug that destroyed worktrees).
        # It is never skipped except by an explicit force override.
        #
        # One narrower path is NOT routed here: _workspace_cleanup.
        # _prune_squash_merged() deletes a branch+worktree directly via
        # git.worktree_remove/branch_delete, but only AFTER is_squash_merged()
        # has confirmed the content is on a remote (merged PR or empty diff vs
        # origin/<default>), so it is low risk. Routing it through this guard
        # would require synthesising a Worktree row and would risk false-
        # blocking legitimately squash-merged branches whose local SHAs differ
        # from the squash commit. Unifying that path is tracked as follow-up
        # (see #706 review) rather than forced here.
        _raise_if_unpushed(str(repo_main), worktree, target)
        # The squash-merge-aware origin/main hygiene gate is stricter: it also
        # blocks pushed-but-unmerged branches. Sync backends and interactive
        # clean-all want it (they clean only on detected merge / orphan reap);
        # the automated FSM teardown path does not (the ticket is MERGED and
        # the work is already preserved on the remote).
        if strict_hygiene:
            _raise_if_genuinely_ahead(str(repo_main), worktree, target)
    errors: list[str] = []
    # #835 — capture before the destructive remove. When force=True the guards
    # above are skipped (the clean-all / abandon reaping path that destroyed a
    # completed-but-uncommitted change set): a dirty or unpushed worktree gets a
    # restorable bundle + working-tree diff under the system temp dir first. A
    # clean, fully-pushed worktree captures nothing — the hard-delete path is
    # unchanged. The captured branch is the EFFECTIVE one (not the drifted slug),
    # so the bundle holds the work removal would actually orphan.
    try:
        capture_recovery_artifact(repo_main, wt_path, worktree, branch=target.label)
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
        logger.exception("recovery capture failed for %s (%s)", worktree.repo_path, target.label)
        if _worktree_has_work_to_lose(wt_path, target):
            msg = (
                f"{worktree.repo_path} ({target.label}): "
                f"refused teardown — recovery capture failed ({exc}) and the worktree has "
                f"unrecoverable work (dirty or unpushed). Kept it on disk at {wt_path}; "
                f"restore or push it, then re-run cleanup."
            )
            raise RuntimeError(msg) from exc
        errors.append(f"recovery capture failed for {target.label}: {exc}")
    if not git.worktree_remove(str(repo_main), wt_path):
        errors.append(f"git worktree remove failed for {wt_path}")
    # Delete the REAL checked-out branch, not the possibly-phantom DB slug. A
    # detached HEAD has no branch to delete (``branch_to_delete is None``).
    if target.branch_to_delete is not None and not git.branch_delete(str(repo_main), target.branch_to_delete):
        errors.append(f"git branch -D failed for {target.branch_to_delete}")
    prek_hook.remove_stale_hooks(str(repo_main), wt_path)
    return errors


def _worktree_has_work_to_lose(wt_path: str, target: _EffectiveTarget) -> bool:
    """Whether removing this worktree would destroy unrecoverable work.

    Re-evaluates the same dirty/unpushed criteria :func:`capture_recovery_artifact`
    uses, but **fails closed** at every step: this guards an irreversible
    ``branch -D`` + ``worktree remove`` after the recovery capture already
    failed, so "couldn't determine" must mean "might lose work", not "safe".

    Operates on ``target`` (the effective branch/HEAD resolved from git), not the
    drifted DB slug. Unpushed commits are checked first via the same fail-open
    probe the capture uses (it returns ``True`` on an inconclusive ``git log``),
    against the worktree-dir ``HEAD`` when present (or the slug in the main clone
    when the dir is gone). Those commits live in the object store, so a missing
    worktree dir does not make them safe — the branch is the only copy.

    The dirty working-tree check runs only when the dir is present and uses the
    strict porcelain probe; an inconclusive ``git status`` (lock contention,
    corrupt index) raises and is treated as "might be dirty".

    Returns ``False`` only when both checks positively confirm there is nothing
    to lose — a clean (or already-gone) worktree whose branch is fully pushed,
    the safe case #835's non-blocking intent still reaps.
    """
    if _has_unpushed_commits(Path(target.probe_repo), target.ref):
        return True
    if not Path(wt_path).is_dir():
        return False
    try:
        return bool(git.status_porcelain_strict(wt_path))
    except CommandFailedError:
        return True


def _guard_or_warn_dirty_worktree(worktree: Worktree, wt_path: str, *, keep_if_dirty: bool, force: bool) -> None:
    """KEEP a dirty worktree when ``keep_if_dirty`` (clean-all, #2243), else warn-and-proceed.

    A worktree with uncommitted changes may be a live one an agent is mid-task in.
    The unattended ``clean-all`` path passes ``keep_if_dirty=True`` so such a
    worktree is never bundle-and-reaped on a merged signal: this raises
    ``RuntimeError`` before any destructive step, which the reaper routes to a
    KEEP-with-warning. ``force=True`` (explicit abandon) overrides the guard. The
    automated FSM teardown of a genuinely-MERGED ticket leaves ``keep_if_dirty``
    off and keeps the documented warn-then-reap-with-recovery-bundle behaviour.
    """
    if not (Path(wt_path).is_dir() and git.status_porcelain(wt_path)):
        return
    if keep_if_dirty and not force:
        msg = (
            f"{worktree.repo_path} ({worktree.branch}): "
            f"refused cleanup — worktree has uncommitted changes (possibly in use). "
            f"Kept it on disk at {wt_path}; commit or discard the changes, then re-run cleanup."
        )
        raise RuntimeError(msg)
    logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)


def _resolve_overlay_or_none(worktree: Worktree) -> "OverlayBase | None":
    """Resolve the worktree's overlay, or ``None`` when it is no longer registered.

    A worktree whose ``overlay`` is absent from the registry (a foreign overlay
    not installed here, or one since uninstalled/renamed) used to abort its own
    teardown — ``get_overlay_for_worktree`` raised ``ImproperlyConfigured`` and
    ``clean-all`` *skipped the row*, leaving the worktree, docker stack and DB
    forever (the "clean-all under-reaps" pain). Returning ``None`` lets
    :func:`cleanup_worktree` run the overlay-agnostic teardown with the #706/#835
    data-loss guards intact, skipping only the overlay-specific steps.
    """
    try:
        return get_overlay_for_worktree(worktree)
    except ImproperlyConfigured as exc:
        logger.warning(
            "cleanup_worktree: overlay %r unavailable for %s (%s) — overlay-free teardown",
            worktree.overlay,
            worktree.repo_path,
            exc,
        )
        return None


def _drop_worktree_db(overlay: "OverlayBase | None", worktree: Worktree, step_errors: list[str]) -> None:
    """Drop the worktree's database, surfacing (never raising) any failure.

    An unregistered overlay can't resolve a custom PG role/host, so fall back to
    the bare ``drop_db`` defaults (postgres/localhost). Best-effort: a failure is
    recorded, never fatal.
    """
    if not worktree.db_name:
        return
    conn = worktree_pg_connection(worktree, overlay=overlay) if overlay is not None else ("", "", {})
    db_user, db_host, db_env = conn
    try:
        drop_db(worktree.db_name, user=db_user, host=db_host, env=db_env or None)
    except Exception as exc:
        logger.exception("dropdb failed for %s (%s)", worktree.db_name, worktree.repo_path)
        step_errors.append(f"dropdb failed for {worktree.db_name}: {exc}")


def _remove_overlay_pass_entry(overlay: "OverlayBase | None", worktree: Worktree, step_errors: list[str]) -> None:
    """Remove the worktree's postgres ``pass`` entry when the overlay opts in; no-op otherwise."""
    if overlay is None or getattr(overlay.config, "teardown_removes_pass_entries", False) is not True:
        return
    ticket = worktree.ticket
    if ticket is None:
        return
    try:
        remove_postgres_pass_entry(ticket.ticket_number)
    except Exception as exc:
        logger.exception("pass-entry removal failed for %s", worktree.repo_path)
        step_errors.append(f"pass-entry removal failed for {ticket.ticket_number}: {exc}")


def cleanup_worktree(
    worktree: Worktree, *, force: bool = False, strict_hygiene: bool = True, keep_if_dirty: bool = False
) -> CleanupResult:
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

    Dirty-worktree guard (``keep_if_dirty``, default off): when set, a worktree
    with uncommitted changes is KEPT — ``RuntimeError`` is raised before any
    destructive step rather than bundle-and-reaping it. ``clean-all`` passes
    this so a live worktree an agent is mid-task in is never deleted on a merged
    signal (#2243); the automated FSM teardown of a genuinely-MERGED ticket
    leaves it off (the recovery-bundle reap of a dirty merged worktree is
    intentional there). ``force=True`` overrides it — the explicit-abandon path
    still bundles and reaps.

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
    overlay = _resolve_overlay_or_none(worktree)

    _guard_or_warn_dirty_worktree(worktree, wt_path, keep_if_dirty=keep_if_dirty, force=force)

    # Stop the docker compose project FIRST so containers don't leak when
    # this path is reached outside the WorktreeTeardownRunner (#1306) —
    # the auto-merged-ticket teardown, `clean-merged`, `clean-all`, and
    # sync backends all funnel through here. Idempotent: docker compose
    # down on a project with no containers is a no-op.
    from teatree.core.runners.worktree_start import docker_compose_down  # noqa: PLC0415

    docker_compose_down(compose_project(worktree))

    step_errors: list[str] = []
    run_overlay_cleanup_steps(overlay, worktree, step_errors)

    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    step_errors.extend(_remove_git_worktree(repo_main, wt_path, worktree, force=force, strict_hygiene=strict_hygiene))

    _drop_worktree_db(overlay, worktree, step_errors)
    _remove_overlay_pass_entry(overlay, worktree, step_errors)

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    if overlay is not None:
        label += reap_external_resources(overlay, worktree, step_errors)

    worktree.delete()
    return CleanupResult(label=label, errors=step_errors)
