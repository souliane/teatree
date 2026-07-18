"""Durable e2e work-item recipe + environment ladder + run provenance (#794).

The keystone of ``t3 <overlay> e2e run <work-item>``: one command that runs
the e2e for a work item with auto-provisioning, so testing a ticket never
takes days.

Identity
    The key is the **work item** — teatree's existing Ticket natural key
    (``issue_url``, 1:1 with Ticket). Never the disposable row pk, never a
    branch-name. ``Ticket.objects.resolve(<ref>)`` maps a pk / issue-number /
    issue-URL to the Ticket.

Durable record (DB, keyed by issue_url)
    Stored under ``Ticket.extra['e2e_recipe']`` — the teatree DB is the
    system of record (a DB, not a cache: if lost, re-establish a baseline by
    running against current ``origin/main``). Written through
    ``Ticket.merge_extra`` so a concurrent ``extra`` writer's key survives.

Default environment ladder (``--at last-green|main`` overrides)
    1. recipe's repo set fully present on disk (reconcile-on-read) → run the existing workspace **as-is**.
    2. not fully present, ``last_green`` set → provision each repo at its last *successful* (green) SHA → run.
    3. not fully present, no ``last_green`` → provision each repo at current ``origin/main`` → run.

    On green, the run's SHA-set becomes the new ``last_green``. A failed run
    records provenance but never becomes the baseline.

Reconcile-on-read
    A DB ``Worktree`` row whose recorded path no longer exists on disk is
    *stale* — it must NOT be adopted as "existing". The ladder falls through
    rather than running against a path that is gone (the SSOT lesson: never
    "DB says X, disk says Y, run anyway").
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.models import Ticket, Worktree
from teatree.core.models.types import E2ELastRunSerialized, E2ERecipeSerialized, E2ERepoEntrySerialized

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

_RECIPE_KEY = "e2e_recipe"


@dataclass(frozen=True)
class RepoEntry:
    """One repo in the work item's multi-repo set, with its last-green SHA."""

    repo: str
    branch: str = ""
    last_green_sha: str = ""

    def serialize(self) -> E2ERepoEntrySerialized:
        return E2ERepoEntrySerialized(
            repo=self.repo,
            branch=self.branch,
            last_green_sha=self.last_green_sha,
        )

    @classmethod
    def deserialize(cls, raw: E2ERepoEntrySerialized) -> "RepoEntry":
        return cls(
            repo=str(raw.get("repo", "")),
            branch=str(raw.get("branch", "")),
            last_green_sha=str(raw.get("last_green_sha", "")),
        )


@dataclass
class E2ERecipe:
    """The durable, set-wise, multi-repo e2e recipe for a work item."""

    repos: list[RepoEntry] = field(default_factory=list)
    last_run: E2ELastRunSerialized | None = None

    def serialize(self) -> E2ERecipeSerialized:
        out = E2ERecipeSerialized(repos=[r.serialize() for r in self.repos])
        if self.last_run is not None:
            out["last_run"] = self.last_run
        return out

    @classmethod
    def deserialize(cls, raw: E2ERecipeSerialized | None) -> "E2ERecipe":
        if not raw:
            return cls()
        return cls(
            repos=[RepoEntry.deserialize(r) for r in raw.get("repos", [])],
            last_run=raw.get("last_run"),
        )


@dataclass(frozen=True)
class EnvResolution:
    """Outcome of the default environment ladder for one work item.

    ``rung`` is the ladder step taken: ``existing`` (run the workspace as-is),
    ``last_green`` (provision each repo at its recorded green SHA), or
    ``main`` (provision each repo at ``origin/main``).

    For ``existing``, ``repo_dirs`` maps repo → on-disk worktree path. For the
    provisioning rungs, ``provision_at`` maps repo → the ref to materialise.
    """

    rung: str
    repo_dirs: dict[str, str] = field(default_factory=dict)
    provision_at: dict[str, str] = field(default_factory=dict)


def load_recipe(ticket: Ticket) -> E2ERecipe:
    """Load the durable recipe for *ticket* (empty when none recorded)."""
    raw = (ticket.extra or {}).get(_RECIPE_KEY)
    return E2ERecipe.deserialize(raw)


def save_recipe(ticket: Ticket, recipe: E2ERecipe) -> None:
    """Persist *recipe* under ``Ticket.extra['e2e_recipe']`` (locked RMW)."""
    ticket.merge_extra(set_keys={_RECIPE_KEY: recipe.serialize()})


def _workspace_repo_dirs(ticket: Ticket) -> dict[str, str]:
    """Reconcile-on-read: repo → path for worktrees that exist on disk NOW.

    A ``Worktree`` row whose recorded ``worktree_path`` is gone is stale and
    is dropped here — the ladder must never adopt a path that disappeared.
    """
    dirs: dict[str, str] = {}
    for wt in Worktree.objects.filter(ticket=ticket):
        path = wt.worktree_path
        if path and Path(path).exists():
            dirs[str(wt.repo_path)] = path
    return dirs


def _recipe_repo_names(ticket: Ticket, recipe: E2ERecipe) -> list[str]:
    """The work item's multi-repo set, in priority order.

    The recipe is authoritative once recorded; before a first green run it
    falls back to ``ticket.repos`` (the scoped repo list) and finally to the
    repos of any already-registered ``Worktree`` rows so an explicit
    ``--at`` can still name every repo to reprovision.
    """
    if recipe.repos:
        return [r.repo for r in recipe.repos]
    names = [str(r) for r in (ticket.repos or [])]
    if names:
        return names
    seen: dict[str, None] = {}
    for wt in Worktree.objects.filter(ticket=ticket).order_by("pk"):
        seen.setdefault(str(wt.repo_path), None)
    return list(seen)


def resolve_environment(ticket: Ticket, *, at: str = "") -> EnvResolution:
    """Apply the default environment ladder for *ticket*.

    ``at`` is the explicit ``--at`` override: ``"main"`` forces the
    ``origin/main`` rung, ``"last-green"`` forces the recorded-green rung —
    both skip the use-existing rung even when a workspace is present.
    """
    recipe = load_recipe(ticket)
    repo_names = _recipe_repo_names(ticket, recipe)
    normalized = at.strip().lower()

    if normalized in {"main", "origin/main"}:
        return EnvResolution(rung="main", provision_at=dict.fromkeys(repo_names, "origin/main"))

    green_by_repo = {r.repo: r.last_green_sha for r in recipe.repos if r.last_green_sha}
    if normalized in {"last-green", "last_green"}:
        return EnvResolution(rung="last_green", provision_at=dict(green_by_repo))

    on_disk = _workspace_repo_dirs(ticket)
    if repo_names and all(name in on_disk for name in repo_names):
        return EnvResolution(rung="existing", repo_dirs={n: on_disk[n] for n in repo_names})

    if green_by_repo:
        return EnvResolution(rung="last_green", provision_at=dict(green_by_repo))

    return EnvResolution(rung="main", provision_at=dict.fromkeys(repo_names, "origin/main"))


@dataclass(frozen=True)
class RunProvenance:
    """Which vanilla spec a run exercised + where its evidence lives, for DB-only reproducibility (#272, #3331).

    ``spec_path`` is the exact spec that ran; ``manifest_entry`` the
    overlay-resolved manifest entry id (e.g. a CI lane); ``artifacts_dir`` the
    out-of-repo artifacts root the runner exported for the run (so
    ``post-test-plan --from-seams`` locates the captures after cleanup). All
    overlay-agnostic strings core never parses; empty values are dropped rather
    than stored, so an overlay with no per-spec manifest records exactly the
    pre-#272 shape (the default ``RunProvenance()`` is the no-op).
    """

    spec_path: str = ""
    manifest_entry: str = ""
    artifacts_dir: str = ""


_NO_PROVENANCE = RunProvenance()


def resolve_run_provenance(overlay: "OverlayBase", spec_path: str) -> RunProvenance:
    """Build the #272 run provenance for *spec_path*, asking *overlay* for its lane.

    Empty ``spec_path`` (no per-spec run) yields the no-op provenance; otherwise
    the overlay maps the spec to its manifest entry id via
    :meth:`OverlayE2E.run_provenance`.
    """
    if not spec_path:
        return _NO_PROVENANCE
    return RunProvenance(spec_path=spec_path, manifest_entry=overlay.e2e.run_provenance(spec_path))


def record_run(
    ticket: Ticket,
    *,
    result: str,
    per_repo_shas: dict[str, str],
    env: str = "local",
    provenance: RunProvenance = _NO_PROVENANCE,
) -> None:
    """Record run provenance on the durable recipe (#794, #88, #272, #3331).

    ``{result, timestamp, per_repo_shas, env}`` is written to ``last_run`` so
    a run is auditable after the workspace is cleaned. On a **green** run the
    SHA-set is promoted to ``last_green`` (the new baseline); a failed run
    records provenance but never moves the baseline.

    ``provenance.artifacts_dir`` (#3331) is the out-of-repo artifacts root the
    runner exported; recorded so ``post-test-plan --from-seams`` (#3329) defaults
    the artifacts dir to the run's. Empty is dropped rather than stored.

    ``env`` is the environment the run executed against — ``"local"``
    (teatree-managed local stack, the default since ``e2e run`` resolves an
    on-disk workspace) or ``"dev"`` (a deployed dev run). The DoD gate (#88)
    reads it: only a *local* green run satisfies the pre-ship requirement,
    so a dev-after-merge run records provenance without unblocking the gate.

    ``provenance`` (#272) records which vanilla spec ran and its
    overlay-resolved manifest entry id so the run is reproducible from the DB
    record alone — empty fields are omitted (default ``RunProvenance()`` is the
    pre-#272 shape).
    """
    recipe = load_recipe(ticket)
    last_run = E2ELastRunSerialized(
        result=result,
        timestamp=timezone.now().isoformat(),
        per_repo_shas=dict(per_repo_shas),
        env=env,
    )
    if provenance.spec_path:
        last_run["spec_path"] = provenance.spec_path
    if provenance.manifest_entry:
        last_run["manifest_entry"] = provenance.manifest_entry
    if provenance.artifacts_dir:
        last_run["artifacts_dir"] = provenance.artifacts_dir
    recipe.last_run = last_run
    if result == "green":
        by_repo = {r.repo: r for r in recipe.repos}
        for repo, sha in per_repo_shas.items():
            existing = by_repo.get(repo)
            branch = existing.branch if existing else ""
            by_repo[repo] = RepoEntry(repo=repo, branch=branch, last_green_sha=sha)
        recipe.repos = list(by_repo.values())
    save_recipe(ticket, recipe)
