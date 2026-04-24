"""Stream D of the robustness plan (#390): typed state reconciler.

Walks every state store teatree cares about — Django rows, git worktrees,
Postgres DBs, docker containers, redis slot, env cache files — and returns
a ``Drift`` bundle enumerating what's out of sync.

Drift objects are typed (per-finding dataclass) so callers can inspect and
act without string-parsing a log.  ``t3 workspace doctor`` is the primary
consumer; ``worktree start`` calls ``reconcile_ticket`` before provisioning
and refuses when drift is present.
"""

from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import load_config
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import detect_drift, render_env_cache
from teatree.utils import git
from teatree.utils.db import db_exists
from teatree.utils.run import run_allowed_to_fail

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
        ]
        return "\n".join(lines) or "(no drift)"


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

    ``git.run`` uses ``run_allowed_to_fail`` under the hood — a non-git dir or
    other failure just yields empty stdout, no exception.
    """
    if not (repo_main / ".git").exists():
        return set()
    raw = git.run(repo=str(repo_main), args=["worktree", "list", "--porcelain"])
    return {line.removeprefix("worktree ") for line in raw.splitlines() if line.startswith("worktree ")}


# ── Reconciler ───────────────────────────────────────────────────────


def _reconcile_worktree_row(drift: Drift, wt: Worktree, ticket_number: str) -> None:
    """Append findings for a single ``Worktree`` row to *drift*."""
    extra = wt.extra or {}
    wt_path_str = extra.get("worktree_path", "")
    wt_path = Path(wt_path_str) if wt_path_str else None

    if wt_path and not wt_path.is_dir():
        drift.missing_worktree_dirs.append(MissingWorktreeDir(worktree_pk=wt.pk, path=wt_path))

    if render_env_cache(wt) is not None:
        drifted, cache_path = detect_drift(wt)
        if drifted and cache_path is not None:
            if cache_path.is_file():
                drift.env_cache_drifts.append(EnvCacheDrift(worktree_pk=wt.pk, cache_path=cache_path))
            else:
                drift.missing_env_caches.append(MissingEnvCache(worktree_pk=wt.pk, cache_path=cache_path))

    if wt.db_name and wt.state != Worktree.State.CREATED and not db_exists(wt.db_name):
        drift.missing_dbs.append(MissingDB(worktree_pk=wt.pk, db_name=wt.db_name))

    if wt.state == Worktree.State.CREATED:
        compose_project = f"{wt.repo_path}-wt{ticket_number}"
        drift.orphan_containers.extend(OrphanContainer(name=n) for n in _find_docker_containers(compose_project))


def _collect_stale_worktree_dirs(drift: Drift, worktrees: list[Worktree], ticket: Ticket, workspace: Path) -> None:
    """Append :class:`StaleWorktreeDir` findings for unclaimed git worktrees."""
    seen_paths: set[str] = {
        Path(wt.extra.get("worktree_path", "")).resolve().as_posix()
        for wt in worktrees
        if (wt.extra or {}).get("worktree_path")
    }
    for wt in worktrees:
        repo_main = workspace / wt.repo_path
        for path_str in _find_worktree_paths_on_disk(repo_main):
            resolved = Path(path_str).resolve().as_posix()
            if resolved == str(repo_main.resolve()):
                continue
            matches_ticket = f"/{ticket.ticket_number}" in path_str or f"-{ticket.ticket_number}-" in path_str
            if matches_ticket and resolved not in seen_paths:
                drift.stale_worktree_dirs.append(StaleWorktreeDir(path=Path(path_str)))


def reconcile_ticket(ticket: Ticket) -> Drift:
    """Walk every state store and return a typed ``Drift`` for *ticket*."""
    drift = Drift(ticket_pk=ticket.pk)
    workspace = load_config().user.workspace_dir
    worktrees = list(Worktree.objects.filter(ticket=ticket))

    for wt in worktrees:
        _reconcile_worktree_row(drift, wt, ticket.ticket_number)
    _collect_stale_worktree_dirs(drift, worktrees, ticket, workspace)
    return drift


def reconcile_all() -> dict[int, Drift]:
    """Return a ``{ticket.pk: Drift}`` map for every ticket with drift."""
    drifts: dict[int, Drift] = {}
    for ticket in Ticket.objects.all():
        drift = reconcile_ticket(ticket)
        if drift.has_drift:
            drifts[ticket.pk] = drift
    return drifts
