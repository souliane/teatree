"""Stream D of the robustness plan (#390): typed state reconciler.

Walks every state store teatree cares about — Django rows, git worktrees,
Postgres DBs, docker containers, env cache files — and returns
a ``Drift`` bundle enumerating what's out of sync.

Drift objects are typed (per-finding dataclass) so callers can inspect and
act without string-parsing a log.  ``t3 teatree workspace doctor`` is the
primary consumer; ``recover`` surfaces the drifted ticket pks via
``reconcile_all``.
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.config import clone_root, worktree_root
from teatree.core.models import Ticket, Worktree
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.worktree.branch_classification import RedundancyVerdict, branch_redundancy
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.worktree_collision import find_foreign_issue_worktrees
from teatree.core.worktree.worktree_env import compose_project, detect_drift, render_env_cache, worktree_pg_connection
from teatree.core.worktree.worktree_paths import paths_match, ticket_dir_for
from teatree.utils import git
from teatree.utils.db import db_exists
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

# ── Findings ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrphanContainer:
    """Docker container that matches the worktree's compose prefix but no Worktree row claims it."""

    name: str


@dataclass(frozen=True, slots=True)
class OrphanDB:
    """Postgres database named like a worktree DB but with no backing Worktree row."""

    db_name: str


@dataclass(frozen=True, slots=True)
class StaleWorktreeDir:
    """Filesystem path that still contains a git worktree but no Worktree row."""

    path: Path


@dataclass(frozen=True, slots=True)
class MissingWorktreeDir:
    """Worktree row claims a path that no longer exists on disk."""

    worktree_pk: int
    path: Path


@dataclass(frozen=True, slots=True)
class MissingEnvCache:
    """Worktree is provisioned but the env cache file is not on disk."""

    worktree_pk: int
    cache_path: Path


@dataclass(frozen=True, slots=True)
class EnvCacheDrift:
    """Env cache file on disk differs from a fresh DB render."""

    worktree_pk: int
    cache_path: Path


@dataclass(frozen=True, slots=True)
class MissingDB:
    """Worktree row references a db_name that doesn't exist in Postgres."""

    worktree_pk: int
    db_name: str


@dataclass(frozen=True, slots=True)
class UnresolvableOverlay:
    """Worktree row references an overlay not installed in this environment.

    Surfacing-only: the overlay's docker/DB stores can't be reconciled without
    the overlay, but recording the finding keeps the sweep from aborting on the
    row — the overlay-independent checks still apply, and other tickets are
    still reconciled.
    """

    worktree_pk: int
    overlay: str
    reason: str


# ── Work-tracking-truth findings (SELFCATCH-1) ───────────────────────
# The three drift classes the factory stayed blind to until a human asked:
# committed-but-unpushed, done-but-unmerged, and duplicate-scope. Each is built
# from an existing probe (``git.commits_absent_from_all_remotes`` /
# ``branch_redundancy`` / ``find_foreign_issue_worktrees``); the reconciler runs
# them autonomously so the orphaned work surfaces at the next tick, unprompted.


@dataclass(frozen=True, slots=True)
class UnpushedWork:
    """A live worktree carrying commits that exist on NO remote (committed-but-unpushed).

    ``shas`` are the ``"<sha> <subject>"`` lines from
    :func:`git.commits_absent_from_all_remotes`. ``probe_error`` is set instead
    when the pushed-state probe was inconclusive — a fail-closed finding (the
    worktree is surfaced, never assumed clean when it cannot be verified).
    """

    worktree_pk: int
    branch: str
    shas: list[str] = field(default_factory=list)
    probe_error: str = ""


@dataclass(frozen=True, slots=True)
class DoneButUnmerged:
    """A ticket in a done-claiming terminal state whose branch never merged.

    The ticket reads MERGED/RETROSPECTED/DELIVERED, yet no ``MergeAudit`` row
    records a merged SHA AND its branch is not provably upstream — the
    believe-done-what-isn't class (a ticket committed, tested, marked done, but
    left unpushed/unmerged).
    """

    ticket_pk: int
    branch: str
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateScope:
    """More than one worktree directory exists for the same issue scope.

    ``paths`` are the distinct ``<N>-*`` worktree dirs found for ``issue_number``
    (the ticket's own dir plus every foreign one) — the blind-redo class where two
    branches/worktrees serve one ticket-scope.
    """

    issue_number: str
    paths: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Drift:
    """Bundle of per-store findings for a ticket.

    ``has_drift`` is true when any list is non-empty.  Each field is the
    ordered list of typed findings; callers pattern-match to act.
    """

    ticket_pk: int
    orphan_containers: list[OrphanContainer] = field(default_factory=list)
    orphan_dbs: list[OrphanDB] = field(default_factory=list)
    stale_worktree_dirs: list[StaleWorktreeDir] = field(default_factory=list)
    missing_worktree_dirs: list[MissingWorktreeDir] = field(default_factory=list)
    missing_env_caches: list[MissingEnvCache] = field(default_factory=list)
    env_cache_drifts: list[EnvCacheDrift] = field(default_factory=list)
    missing_dbs: list[MissingDB] = field(default_factory=list)
    unresolvable_overlays: list[UnresolvableOverlay] = field(default_factory=list)
    unpushed_work: list[UnpushedWork] = field(default_factory=list)
    done_but_unmerged: list[DoneButUnmerged] = field(default_factory=list)
    duplicate_scopes: list[DuplicateScope] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return any(
            (
                self.orphan_containers,
                self.orphan_dbs,
                self.stale_worktree_dirs,
                self.missing_worktree_dirs,
                self.missing_env_caches,
                self.env_cache_drifts,
                self.missing_dbs,
                self.unresolvable_overlays,
                self.unpushed_work,
                self.done_but_unmerged,
                self.duplicate_scopes,
            ),
        )

    def format(self) -> str:
        """Human-readable one-drift-per-line summary."""
        lines: list[str] = [
            *(f"orphan-container: {c.name}" for c in self.orphan_containers),
            *(f"orphan-db: {d.db_name}" for d in self.orphan_dbs),
            *(f"stale-worktree-dir: {d.path}" for d in self.stale_worktree_dirs),
            *(f"missing-worktree-dir: wt#{d.worktree_pk} {d.path}" for d in self.missing_worktree_dirs),
            *(f"missing-env-cache: wt#{d.worktree_pk} {d.cache_path}" for d in self.missing_env_caches),
            *(f"env-cache-drift: wt#{d.worktree_pk} {d.cache_path}" for d in self.env_cache_drifts),
            *(f"missing-db: wt#{d.worktree_pk} {d.db_name}" for d in self.missing_dbs),
            *(
                f"unresolvable-overlay: wt#{u.worktree_pk} {u.overlay!r} ({u.reason})"
                for u in self.unresolvable_overlays
            ),
            *(f"unpushed-work: wt#{u.worktree_pk} {u.branch} ({_unpushed_detail(u)})" for u in self.unpushed_work),
            *(f"done-but-unmerged: ticket#{d.ticket_pk} {d.branch} — {d.reason}" for d in self.done_but_unmerged),
            *(
                f"duplicate-scope: issue {d.issue_number} at {', '.join(str(p) for p in d.paths)}"
                for d in self.duplicate_scopes
            ),
        ]
        return "\n".join(lines) or "(no drift)"


def _unpushed_detail(finding: UnpushedWork) -> str:
    """One-line reason for an :class:`UnpushedWork` finding (commits vs probe error)."""
    if finding.shas:
        return f"{len(finding.shas)} commit(s) absent from all remotes"
    return f"pushed-state probe inconclusive: {finding.probe_error}"


# ── Per-store finders ────────────────────────────────────────────────


def _find_docker_containers(project_prefix: str) -> list[str]:
    """Return names of running+stopped docker containers whose project matches *project_prefix*."""
    if not project_prefix:
        return []
    result = run_allowed_to_fail(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project_prefix}",
            "--format",
            "{{.Names}}",
        ],
        expected_codes=None,
    )
    if result.returncode != 0:
        return []
    return [name.strip() for name in result.stdout.splitlines() if name.strip()]


def _find_worktree_paths_on_disk(repo_main: Path) -> set[str]:
    """Return the set of worktree paths reported by ``git worktree list`` for *repo_main*.

    A thin derivation of :func:`git.list_worktrees`, which is permissive (a
    non-git dir yields no records, no exception). The :func:`git.is_git_checkout`
    guard rejects a torn-down "hollow" directory before probing.
    """
    if not git.is_git_checkout(repo_main):
        return set()
    return {str(record.path) for record in git.list_worktrees(str(repo_main))}


# ── Reconciler ───────────────────────────────────────────────────────


def _reconcile_worktree_row(drift: Drift, wt: Worktree) -> None:
    """Append findings for a single ``Worktree`` row to *drift*.

    The overlay-independent missing-dir check always runs. The overlay-dependent
    stores (env cache, DB, containers) are isolated: a row whose overlay is not
    installed in this environment degrades to an :class:`UnresolvableOverlay`
    finding instead of aborting the whole sweep.
    """
    extra = wt.extra or {}
    wt_path_str = extra.get("worktree_path", "")
    wt_path = Path(wt_path_str) if wt_path_str else None

    if wt_path and not wt_path.is_dir():
        drift.missing_worktree_dirs.append(MissingWorktreeDir(worktree_pk=wt.pk, path=wt_path))

    try:
        _reconcile_overlay_dependent_stores(drift, wt)
    except ImproperlyConfigured as exc:
        drift.unresolvable_overlays.append(
            UnresolvableOverlay(worktree_pk=wt.pk, overlay=wt.overlay or wt.ticket.overlay, reason=str(exc)),
        )


def _reconcile_overlay_dependent_stores(drift: Drift, wt: Worktree) -> None:
    """Append findings whose detection needs the worktree's overlay (env cache, DB, containers)."""
    if render_env_cache(wt) is not None:
        drifted, cache_path = detect_drift(wt)
        if drifted and cache_path is not None:
            if cache_path.is_file():
                drift.env_cache_drifts.append(EnvCacheDrift(worktree_pk=wt.pk, cache_path=cache_path))
            else:
                drift.missing_env_caches.append(MissingEnvCache(worktree_pk=wt.pk, cache_path=cache_path))

    if wt.db_name and wt.state != Worktree.State.CREATED:
        user, host, env = worktree_pg_connection(wt)
        try:
            db_present = db_exists(wt.db_name, user=user, host=host, env=env or None)
        except (CommandFailedError, FileNotFoundError):
            # A probe that could not reach the server is not evidence the DB is
            # gone — never report a live DB as missing drift (#3094).
            db_present = True
        if not db_present:
            drift.missing_dbs.append(MissingDB(worktree_pk=wt.pk, db_name=wt.db_name))

    if wt.state == Worktree.State.CREATED:
        drift.orphan_containers.extend(OrphanContainer(name=n) for n in _find_docker_containers(compose_project(wt)))


def _collect_stale_worktree_dirs(
    drift: Drift, worktrees: list[Worktree], ticket: Ticket, clone_workspace: Path
) -> None:
    """Append :class:`StaleWorktreeDir` findings for unclaimed git worktrees."""
    stored_paths: list[str] = [
        str(wt.extra["worktree_path"]) for wt in worktrees if (wt.extra or {}).get("worktree_path")
    ]
    # Anchor the ticket-number match on path segments so ``/9`` no longer
    # matches ``/90``: the number must be a whole segment, bounded by ``/`` or
    # ``-`` (or the string ends), never a substring of a longer number.
    ticket_anchor = re.compile(rf"(?:^|[/-]){re.escape(ticket.ticket_number)}(?:[-/]|$)")
    for wt in worktrees:
        repo_main = resolve_clone_path(clone_workspace, wt) or clone_workspace / wt.repo_path
        for path_str in _find_worktree_paths_on_disk(repo_main):
            if paths_match(path_str, repo_main):
                continue
            already_tracked = any(paths_match(path_str, stored) for stored in stored_paths)
            if ticket_anchor.search(path_str) and not already_tracked:
                drift.stale_worktree_dirs.append(StaleWorktreeDir(path=Path(path_str)))


# ── Work-state finders (SELFCATCH-1) ─────────────────────────────────

# States that CLAIM the work shipped: a branch of one of these that never merged
# is DoneButUnmerged drift. IGNORED is abandoned (its branch legitimately never
# merges), so it is excluded — flagging it would be a false positive.
_DONE_CLAIMING_STATES = frozenset(
    {Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED},
)
_FALLBACK_TARGET = "origin/main"


def _unpushed_work_for_worktree(wt: Worktree) -> UnpushedWork | None:
    """UnpushedWork when ``wt``'s HEAD carries commits absent from every remote.

    Probes the live checkout by ``HEAD`` (branch-drift / detached-HEAD safe).
    Fail-closed: an inconclusive probe (``git log`` error — corrupt repo, dangling
    ref) on a real worktree is surfaced with ``probe_error`` set, never a silent
    pass. A fully-pushed HEAD yields an empty list and no finding (no false
    positive on a clean tree). A path that is not a git working tree at all is
    skipped — it holds no commits that could be unpushed, and the infra reconcile
    (missing/stale dir) owns that case.
    """
    wt_path = wt.worktree_path
    if not wt_path or not Path(wt_path).is_dir():
        return None
    if not git.check(repo=wt_path, args=["rev-parse", "--is-inside-work-tree"]):
        return None
    try:
        absent = git.commits_absent_from_all_remotes(wt_path, "HEAD")
    except CommandFailedError as exc:
        return UnpushedWork(worktree_pk=wt.pk, branch=wt.branch, probe_error=str(exc))
    if not absent:
        return None
    return UnpushedWork(worktree_pk=wt.pk, branch=wt.branch, shas=absent)


def _ticket_has_merge_evidence(ticket: Ticket) -> bool:
    """Whether a ``MergeAudit`` with a real merged SHA exists for ``ticket``.

    The spoof-proof evidence the merge keystone writes atomically with the merge —
    the only DB proof that a done-claiming ticket actually merged (§17.4.4).
    """
    return MergeAudit.objects.filter(clear__ticket=ticket).exclude(merged_sha="").exists()


def _default_target_ref(repo: Path) -> str:
    """Resolve ``repo``'s real default branch as an ``origin/<default>`` ref.

    Fail-safe to ``origin/main`` on an unresolvable default — ``branch_redundancy``
    then fails closed (an unresolvable target makes ``git cherry`` inconclusive), so
    a wrong base keeps the conservative not-redundant verdict.
    """
    try:
        return f"origin/{git.default_branch(str(repo))}"
    except (RuntimeError, CommandFailedError):
        return _FALLBACK_TARGET


def _done_but_unmerged_for_ticket(
    ticket: Ticket, worktrees: list[Worktree], clone_workspace: Path
) -> DoneButUnmerged | None:
    """DoneButUnmerged when a done-claiming ticket has no merge evidence and its branch is not upstream.

    Requires a probeable branch to prove the negative: a done-claiming ticket with
    no live worktree is skipped, since MergeAudit-absence alone is ambiguous for
    historical (pre-machinery) merges. Any live branch that ``branch_redundancy``
    proves upstream clears the ticket (it really merged, just without an audit row);
    a not-redundant OR inconclusive branch (fail-closed) is the finding.
    """
    if str(ticket.state) not in _DONE_CLAIMING_STATES:
        return None
    if _ticket_has_merge_evidence(ticket):
        return None
    verdicts: list[tuple[str, RedundancyVerdict]] = []
    for wt in worktrees:
        repo = resolve_clone_path(clone_workspace, wt)
        if repo is None or not repo.is_dir():
            continue
        verdict = branch_redundancy(str(repo), wt.branch, _default_target_ref(repo))
        if verdict.redundant:
            return None
        verdicts.append((wt.branch, verdict))
    if not verdicts:
        return None
    branch, verdict = verdicts[0]
    if verdict.source == "inconclusive":
        reason = "no merge audit and branch merge-state inconclusive (fail-closed)"
    else:
        reason = f"no merge audit and branch has {len(verdict.unique_shas)} unmerged commit(s)"
    return DoneButUnmerged(ticket_pk=ticket.pk, branch=branch, reason=reason)


def _duplicate_scope_for_ticket(
    ticket: Ticket, worktrees: list[Worktree], worktree_workspace: Path
) -> DuplicateScope | None:
    """DuplicateScope when a second ``<N>-*`` worktree dir exists for the ticket's issue scope.

    ``find_foreign_issue_worktrees`` excludes the ticket's OWN issue dir, so a
    normal single-worktree ticket yields no foreign dir and no finding. A ticket
    with no known branch is skipped (no own-dir to exclude).
    """
    issue_number = ticket.ticket_number
    if not issue_number:
        return None
    branch = (ticket.extra or {}).get("branch") or (worktrees[0].branch if worktrees else "")
    if not branch:
        return None
    own = ticket_dir_for(worktree_workspace, branch)
    foreign = find_foreign_issue_worktrees(issue_number, own_path=own, workspace_dir=worktree_workspace)
    if not foreign:
        return None
    return DuplicateScope(issue_number=issue_number, paths=[own, *foreign])


def _collect_work_state_drift(drift: Drift, ticket: Ticket, worktrees: list[Worktree], clone_workspace: Path) -> None:
    """Append the three work-tracking-truth findings (SELFCATCH-1) for one ticket.

    Two distinct workspace roots are threaded here and must not be conflated:
    ``clone_workspace`` (:func:`clone_root`) locates the bare clones the
    done-but-unmerged probe reads, while the duplicate-scope finder walks the
    :func:`worktree_root` tree where the checked-out worktrees live.

    Read-only: every finder SURFACES drift and never mutates — auto-push and
    auto-delete stay gated behind the destructive commands.
    """
    for wt in worktrees:
        finding = _unpushed_work_for_worktree(wt)
        if finding is not None:
            drift.unpushed_work.append(finding)
    done = _done_but_unmerged_for_ticket(ticket, worktrees, clone_workspace)
    if done is not None:
        drift.done_but_unmerged.append(done)
    dup = _duplicate_scope_for_ticket(ticket, worktrees, worktree_root())
    if dup is not None:
        drift.duplicate_scopes.append(dup)


def reconcile_ticket(ticket: Ticket) -> Drift:
    """Walk every state store and return a typed ``Drift`` for *ticket*."""
    drift = Drift(ticket_pk=ticket.pk)
    clone_workspace = clone_root()
    worktrees = list(Worktree.objects.filter(ticket=ticket))

    for wt in worktrees:
        _reconcile_worktree_row(drift, wt)
    _collect_stale_worktree_dirs(drift, worktrees, ticket, clone_workspace)
    _collect_work_state_drift(drift, ticket, worktrees, clone_workspace)
    return drift


# Prefix of every per-worktree Postgres database teatree provisions. A ``wt_*``
# database with no backing Worktree row is a leaked DB (a teardown whose drop
# failed still deleted the row) — the OrphanDB drift class.
_WT_DB_PREFIX = "wt_"

# Global bucket for drift that belongs to no single ticket (orphan DBs whose
# owning row is already gone). ``0`` is never a real ticket pk.
_GLOBAL_DRIFT_KEY = 0


def find_orphan_dbs() -> list[OrphanDB]:
    """Postgres ``wt_*`` databases that no Worktree row references — the OrphanDB producer.

    Gives :class:`OrphanDB` / ``Drift.orphan_dbs`` a producer so ``workspace
    doctor`` surfaces (and ``--fix`` drops) a leaked per-worktree database. When a
    teardown's DB drop fails the Worktree row is still deleted, so the database is
    left referenced by nothing and would otherwise leak forever. Lists ``wt_*``
    databases and subtracts every ``db_name`` a Worktree row records.

    A box with no ``psql`` on PATH — or a server the probe cannot reach — yields
    no findings rather than raising, so the reconcile sweep never aborts on a
    SQLite-only deployment.
    """
    if shutil.which("psql") is None:
        return []
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415 — deferred: keeps import light

    result = run_allowed_to_fail(
        ["psql", "-h", pg_host(), "-U", pg_user(), "-l", "-t", "-A"],
        env=pg_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        return []
    all_dbs = {line.split("|")[0] for line in result.stdout.splitlines() if line}
    wt_dbs = {db for db in all_dbs if db.startswith(_WT_DB_PREFIX)}
    known = set(Worktree.objects.exclude(db_name="").values_list("db_name", flat=True))
    return [OrphanDB(db_name=db) for db in sorted(wt_dbs - known)]


def reconcile_all() -> dict[int, Drift]:
    """Return a ``{ticket.pk: Drift}`` map for every ticket with drift.

    Orphan ``wt_*`` databases (no owning Worktree row) belong to no single ticket,
    so they are collected once and reported under the global sentinel key
    :data:`_GLOBAL_DRIFT_KEY`.
    """
    drifts: dict[int, Drift] = {}
    for ticket in Ticket.objects.all():
        drift = reconcile_ticket(ticket)
        if drift.has_drift:
            drifts[ticket.pk] = drift
    orphan_dbs = find_orphan_dbs()
    if orphan_dbs:
        drifts[_GLOBAL_DRIFT_KEY] = Drift(ticket_pk=_GLOBAL_DRIFT_KEY, orphan_dbs=orphan_dbs)
    return drifts


def reconcile_work_state_ticket(ticket: Ticket) -> Drift:
    """Work-state-only :class:`Drift` for ``ticket`` — no infra (docker/DB/env) probes.

    The loop scanner's entry point: it needs only the three work-tracking-truth
    findings each tick, not the heavier docker/Postgres/env-cache reconcile that
    :func:`reconcile_ticket` runs for ``workspace doctor``.
    """
    drift = Drift(ticket_pk=ticket.pk)
    worktrees = list(Worktree.objects.filter(ticket=ticket))
    _collect_work_state_drift(drift, ticket, worktrees, clone_root())
    return drift


def reconcile_work_state_all() -> dict[int, Drift]:
    """Return a ``{ticket.pk: Drift}`` map for every ticket with a work-state finding."""
    drifts: dict[int, Drift] = {}
    for ticket in Ticket.objects.all():
        drift = reconcile_work_state_ticket(ticket)
        if drift.has_drift:
            drifts[ticket.pk] = drift
    return drifts
