"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands.

The squash-merge-aware classification this module relies on lives in
:mod:`teatree.core.worktree.branch_classification` — the single home of both the
subject pre-filter and the authoritative content gate. This module imports only
the helpers its own data-loss guards call; every other consumer imports the
classifier from ``branch_classification`` directly (no back-compat re-export shim).
The data-loss guards and the worktree-teardown orchestration live here.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

from django.core.exceptions import ImproperlyConfigured

from teatree.config import clone_root
from teatree.core import prek_hook
from teatree.core.cleanup.cleanup_busy_guards import WorktreeBusyError, guard_live_worktree
from teatree.core.cleanup.cleanup_orphan_ref import raise_or_reap_orphan_ref
from teatree.core.cleanup.working_tree_dirt import real_uncommitted_reasons
from teatree.core.models import Worktree
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.worktree._overlay_teardown import reap_external_resources, run_overlay_cleanup_steps
from teatree.core.worktree.branch_classification import (
    _branch_pr_is_merged,
    _branch_tree_matches_squash,
    content_equivalence_blockers,
)
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.worktree_env import compose_project, worktree_pg_connection
from teatree.core.worktree.worktree_paths import worktree_dir_for
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.postgres_secret import remove_postgres_pass_entry
from teatree.utils.run import CommandFailedError

__all__ = [
    "CleanupResult",
    "WorktreeBusyError",
    "cleanup_worktree",
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
    :func:`prefilter_branch_commits_by_subject` is a cheap SUBJECT recognizer — it
    cannot authorize a force-delete, because a genuine un-upstreamed commit whose subject
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
    row created before the path was set, manual provisioning), fall back to the
    shared :func:`worktree_dir_for` ``<workspace>/<branch>/<repo-leaf>`` layout.
    """
    stored = (worktree.extra or {}).get("worktree_path", "")
    if stored:
        return stored
    return str(worktree_dir_for(workspace, worktree.branch, worktree.repo_path))


def _run_data_loss_guards(
    repo_main: Path,
    wt_path: str,
    worktree: Worktree,
    *,
    force: bool,
    strict_hygiene: bool,
) -> None:
    """Run the #706 unpushed guard + optional origin/main hygiene gate, raising to refuse.

    Hoisted ahead of every DESTRUCTIVE teardown step (``docker_compose_down``
    with ``remove_volumes=True``, overlay cleanup, git worktree removal): a
    refused teardown must raise BEFORE anything is destroyed, else a "kept"
    worktree is left with its per-worktree Postgres volume and overlay resources
    already gone — a partially-destroyed worktree the operator asked to keep.

    ``force`` bypasses every guard (the proven-redundant reaper / explicit
    abandon). A missing source repo skips the guards — there is no repo to run
    the origin-relative probe in and nothing to remove.

    #706 — the data-loss guard is the seam every Worktree-row-driven teardown
    caller funnels through (execute_teardown / WorktreeTeardown,
    WorktreeTeardownRunner, clean-merged, clean-all, sync-backend merge cleanup,
    abandon); it blocks removal of commits that exist on no remote at all. The
    squash-merge-aware origin/main hygiene gate is stricter — it also blocks
    pushed-but-unmerged branches; sync backends and interactive clean-all want
    it, the automated FSM teardown path passes ``strict_hygiene=False``.

    One narrower path is NOT routed here: ``_workspace.cleanup._prune_squash_merged``
    deletes a branch+worktree directly via ``git.worktree_remove/branch_delete``,
    but only AFTER ``is_squash_merged()`` confirmed the content is on a remote,
    so it is low risk.
    """
    if force or not repo_main.is_dir():
        return
    target = _effective_target(str(repo_main), wt_path, worktree)
    _raise_if_unpushed(str(repo_main), worktree, target)
    if strict_hygiene:
        _raise_if_genuinely_ahead(str(repo_main), worktree, target)


def _remove_git_worktree(repo_main: Path, wt_path: str, worktree: Worktree) -> list[str]:
    """Remove the git worktree + branch from the source repo, returning any error messages.

    Returns an empty list on success. The source repo missing, the git worktree
    remove failing, or the branch delete failing each yield one entry. Failures
    are surfaced (not raised) so unrelated cleanup steps still run. The data-loss
    guards (:func:`_run_data_loss_guards`) already ran ahead of this step, so
    removal is unconditional here.
    """
    if not repo_main.is_dir():
        return [f"source repo missing at {repo_main}"]
    # Resolve the teardown target from git ONCE, then operate on it throughout.
    # ``Worktree.branch`` (the DB slug) can drift from the branch actually
    # checked out in the worktree; trusting it makes ``branch -D`` no-op on a
    # phantom slug, leaving the real branch dangling after its worktree is
    # removed.
    target = _effective_target(str(repo_main), wt_path, worktree)
    errors: list[str] = []
    if not git.worktree_remove(str(repo_main), wt_path):
        errors.append(f"git worktree remove failed for {wt_path}")
    # Delete the REAL checked-out branch, not the possibly-phantom DB slug. A
    # detached HEAD has no branch to delete (``branch_to_delete is None``).
    if target.branch_to_delete is not None and not git.branch_delete(str(repo_main), target.branch_to_delete):
        errors.append(f"git branch -D failed for {target.branch_to_delete}")
    # Best-effort, per-step isolated: a benign hook-cleanup raise (e.g. a
    # vanished path/CWD mid-teardown, #2692) is surfaced — never propagated past
    # this step, which would skip the DB drop, ``Worktree`` row delete and reap
    # ordered after it in ``cleanup_worktree``.
    try:
        prek_hook.remove_stale_hooks(str(repo_main), wt_path)
    except OSError as exc:
        errors.append(f"stale prek hook cleanup failed for {wt_path}: {exc}")
    return errors


def _guard_or_warn_dirty_worktree(
    worktree: Worktree, wt_path: str, target: _EffectiveTarget, *, keep_if_dirty: bool, force: bool
) -> None:
    """KEEP a dirty worktree when ``keep_if_dirty`` (the fail-closed default), else warn-and-proceed.

    A worktree with uncommitted changes may be a live one an agent is mid-task
    in, and those edits are on no remote — a board "Done" event or an unattended
    teardown would wipe them with no salvage. ``keep_if_dirty`` defaults ``True``
    (fail-closed): a dirty worktree raises ``RuntimeError`` before any destructive
    step, which the reaper / sync backend routes to a KEEP-with-warning. Only an
    explicit ``keep_if_dirty=False`` caller warns-and-proceeds, and ``force=True``
    (the proven-redundant reaper / explicit abandon) overrides the guard entirely.

    "Dirty" is REAL uncommitted work — :func:`real_uncommitted_reasons` ignores the
    regenerable env cache every provisioned worktree carries and the "every tracked
    file reads as a staged add" noise of a dangling-HEAD (post-merge branch-ref
    deletion) worktree. A raw ``git status --porcelain`` would false-positive on
    both, refusing teardown of every normally-provisioned worktree and every
    legitimate post-merge orphan; the shared probe fails CLOSED only on GENUINE
    edits.
    """
    reasons = real_uncommitted_reasons(wt_path, target)
    if not reasons:
        return
    if keep_if_dirty and not force:
        msg = (
            f"{worktree.repo_path} ({worktree.branch}): "
            f"refused cleanup — worktree has uncommitted changes (possibly in use): {'; '.join(reasons)}. "
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
        # Keyed on the ticket pk (the canonical, unique key), matching
        # ``Worktree.pass_key`` the env render wrote into the cache.
        remove_postgres_pass_entry(ticket.pk)
    except Exception as exc:
        logger.exception("pass-entry removal failed for %s", worktree.repo_path)
        step_errors.append(f"pass-entry removal failed for ticket #{ticket.pk}: {exc}")


def cleanup_worktree(
    worktree: Worktree,
    *,
    force: bool = False,
    strict_hygiene: bool = True,
    keep_if_dirty: bool = True,
    respect_liveness: bool = True,
) -> CleanupResult:
    """Remove a single worktree: git worktree, branch, DB, overlay cleanup.

    Deletes the Worktree record from the database and returns a
    :class:`CleanupResult`. Individual teardown-step failures are captured into
    ``result.errors`` and the remaining steps still run — collect-and-surface,
    never crash mid-teardown leaving other resources orphaned (#877). The caller
    routes ``result.errors`` to its visible channel (``SyncResult.errors`` for
    sync backends, runner detail for runners).

    Several guards protect against losing work; all are bypassed by an explicit
    ``force=True``.

    Data-loss guard (#706, always on): raises ``RuntimeError`` when the branch
    has commits on NO remote ref — removing the worktree would destroy them
    irrecoverably.

    Hygiene gate (``strict_hygiene``, default on): additionally raises when the
    branch is genuinely ahead of ``origin/main`` and not squash-merged — see
    :func:`_raise_if_genuinely_ahead`. Sync backends and interactive
    ``clean-all`` keep it on; the FSM teardown path passes ``strict_hygiene=False``.

    Dirty-worktree guard (``keep_if_dirty``, default ON — fail-closed): a
    worktree with uncommitted changes is KEPT — ``RuntimeError`` is raised before
    any destructive step rather than reaping it — see
    :func:`_guard_or_warn_dirty_worktree`. Uncommitted edits are on no remote, so
    an unattended teardown (sync-backend "Done" cleanup, FSM teardown) must never
    wipe them by default. Only an explicit ``keep_if_dirty=False`` caller
    warns-and-proceeds; ``force=True`` (the proven-redundant reaper / explicit
    abandon) overrides it.

    There is NO recovery snapshot: ``force=True`` hard-deletes. It is reached only
    from a deliberate destroy — the done-worktree reaper after the
    analyze-before-wipe step PROVED every change redundant, or the interactive
    abandon path after a human chose to discard. Potentially-needed work is KEPT by
    those callers, never force-destroyed. Pass ``force=True`` only from such
    trusted callers (the proven reaper, an explicit operator override, tests).

    Liveness guard (``respect_liveness``, default on, #291/#2243): OPPORTUNISTIC
    reapers KEEP a worktree under live work (raising :class:`WorktreeBusyError`),
    so the irreversible teardown never protects LESS than the reversible
    idle-stack reaper — see :func:`guard_live_worktree`. FSM teardown passes
    ``respect_liveness=False``; ``force=True`` bypasses it.
    """
    # Liveness FIRST: a busy worktree short-circuits before resolving paths,
    # overlay, or touching docker — the cheapest possible KEEP for live work.
    guard_live_worktree(worktree, respect_liveness=respect_liveness, force=force)

    workspace = clone_root()
    wt_path = _resolve_worktree_path(workspace, worktree)
    overlay = _resolve_overlay_or_none(worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path

    # Refuse-before-destroy: the dirty-worktree guard and the #706/#2609 git
    # data-loss guards run BEFORE any destructive step (docker volume removal,
    # overlay cleanup, git worktree removal). A refused teardown must leave the
    # worktree fully intact — a guard that fired only after `docker compose down
    # --volumes` would have already deleted the per-worktree Postgres volume of a
    # worktree it then "kept". Both probe the SAME git-resolved effective target
    # (immune to a drifted DB slug, aware of a dangling post-merge HEAD).
    target = _effective_target(str(repo_main), wt_path, worktree)
    _guard_or_warn_dirty_worktree(worktree, wt_path, target, keep_if_dirty=keep_if_dirty, force=force)
    _run_data_loss_guards(repo_main, wt_path, worktree, force=force, strict_hygiene=strict_hygiene)

    # Stop the docker compose project so containers don't leak when this path is
    # reached outside the WorktreeTeardownRunner (#1306) — the FSM teardown,
    # `clean-all`, and sync backends all funnel through here. The done-wipe owns
    # the worktree's docker resources, so it removes the VOLUMES too (a reaped
    # worktree's volumes are a slow disk leak). Idempotent: down on a project with
    # no containers is a no-op.
    from teatree.core.runners.worktree_start import docker_compose_down  # noqa: PLC0415 — deferred: call-time import

    docker_compose_down(compose_project(worktree), remove_volumes=True)

    step_errors: list[str] = []
    run_overlay_cleanup_steps(overlay, worktree, step_errors)

    step_errors.extend(_remove_git_worktree(repo_main, wt_path, worktree))

    _drop_worktree_db(overlay, worktree, step_errors)
    _remove_overlay_pass_entry(overlay, worktree, step_errors)

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    if overlay is not None:
        label += reap_external_resources(overlay, worktree, step_errors)

    worktree.delete()
    return CleanupResult(label=label, errors=step_errors)
