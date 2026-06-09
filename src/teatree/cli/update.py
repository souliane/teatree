"""t3 update — sync teatree core + registered overlays to their default branch.

Updating is a *mutating, network, version-changing* operation, deliberately
kept separate from the idempotent ``t3 setup`` bootstrap.  ``t3 update`` reuses
the setup/reinstall step at the end; ``t3 setup`` never reaches into ``update``.

For teatree core (``$T3_REPO``) and every registered overlay repo, this:

1. ``git fetch`` the origin.
2. Resolves the default branch from ``origin/HEAD``.
3. Skips a non-default-branch / no-upstream checkout for an overlay; for
    the primary/running clone those same states FAIL LOUD (#2134, the
    running editable ``t3`` must never silently rot behind origin). A
    tracked-dirty tree is refused loudly. Untracked-only files do not block.
4. Otherwise ``git pull --ff-only`` — fast-forward only, never merge/rebase.
5. Reinstalls advanced editable installs, then runs ``t3 setup``.
6. Probes the teatree self-DB (``python -m teatree migrate --check`` in the
    *runtime* interpreter) and applies pending migrations non-destructively
    — gated on *migrations actually pending*, NOT on whether a repo advanced
    this run, so an interrupted prior run / out-of-band ff-pull can't leave a
    stale self-DB (#929). Running in the runtime process (not ``uv
    --directory <clone>``) guarantees it migrates the DB the runtime ``t3``
    actually resolves, not an auto-isolated sibling DB (#126).
7. Prints a per-repo summary; exits non-zero on a hard repo failure OR a
    self-DB left unmigrated (fail-closed, consistent with #870).

This module is a top-level Typer group reached through the typer runner
directly (sibling of ``t3 setup`` / ``t3 doctor``), so it raises
``typer.Exit(code=N)`` — *not* ``SystemExit`` (which is for ``TyperCommand``
groups reached via Django ``call_command``; see ``skills/teatree`` § "CLI exit
codes").  Precedent: ``cli/setup.py`` ``_validate_repo`` → ``raise typer.Exit``.
"""

import enum
import shutil
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.self_update import ReinstallResult, SubprocessRunner, ensure_self_db_migrated, reinstall_running_editable
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

__all__ = [
    "ReinstallResult",
    "RepoUpdate",
    "SubprocessRunner",
    "UpdateStatus",
    "ensure_self_db_migrated",
    "reinstall_running_editable",
    "update_app",
    "update_repo",
]

update_app = typer.Typer(
    help="Sync teatree core and registered overlays to their default branch.",
    invoke_without_command=True,
)

# The configured main clone and the work-tree the interpreter actually imports
# ``teatree`` from. These are the *primary* clones — the editable ``t3`` the
# agent runs — so a non-default-branch / no-upstream checkout is a fail-loud
# currency hazard there, not a soft overlay skip (#2134).
_CORE_REPO_NAME = "teatree"
_RUNNING_REPO_NAME = "teatree (running)"
_PRIMARY_REPO_NAMES = frozenset({_CORE_REPO_NAME, _RUNNING_REPO_NAME})


class UpdateStatus(enum.Enum):
    """Outcome of attempting to update a single repo."""

    UPDATED = "updated"
    UP_TO_DATE = "up-to-date"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class RepoUpdate:
    """The result of attempting to update one repo.

    ``is_error`` is the single source of truth for the process exit code: only
    a hard :class:`UpdateStatus.FAILED` is an error — a deliberate skip is not.
    """

    name: str
    status: UpdateStatus
    old_sha: str = ""
    new_sha: str = ""
    reason: str = ""
    advanced: int = 0

    @property
    def is_error(self) -> bool:
        return self.status is UpdateStatus.FAILED

    @property
    def summary_line(self) -> str:
        if self.status is UpdateStatus.UPDATED:
            plural = "commit" if self.advanced == 1 else "commits"
            return f"OK    {self.name}: +{self.advanced} {plural} ({self.old_sha} -> {self.new_sha})"
        if self.status is UpdateStatus.UP_TO_DATE:
            return f"OK    {self.name}: up-to-date"
        if self.status is UpdateStatus.SKIPPED:
            return f"SKIP  {self.name}: skipped ({self.reason})"
        return f"FAIL  {self.name}: {self.reason}"


def _git(repo: Path, *args: str, expected_codes: tuple[int, ...] | None = (0,)) -> CompletedProcess[str]:
    """Run ``git`` in *repo* via the audited subprocess wrapper.

    ``expected_codes=None`` accepts any exit code so the caller can branch on
    it instead of catching an exception.
    """
    return run_allowed_to_fail(["git", *args], cwd=repo, expected_codes=expected_codes)


def _short_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def _commit_count(repo: Path, old_sha: str, new_sha: str) -> int:
    """Count commits the ff-pull added (``git rev-list --count old..new``).

    Both endpoints are SHAs this function's caller just resolved from the
    repo's own HEAD before and after the fast-forward, so the range is always
    valid and the audited ``git`` runs with the strict default exit code.
    """
    return int(_git(repo, "rev-list", "--count", f"{old_sha}..{new_sha}").stdout.strip())


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _default_branch(repo: Path) -> str | None:
    """Resolve the default branch from ``origin/HEAD`` (e.g. ``main``).

    Returns ``None`` when ``origin/HEAD`` is unset — the repo has no
    discoverable default branch and must be skipped.
    """
    result = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", expected_codes=None)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # "refs/remotes/origin/main" -> "main"
    return result.stdout.strip().rsplit("/", 1)[-1]


def _has_origin_remote(repo: Path) -> bool:
    result = _git(repo, "remote", expected_codes=None)
    return "origin" in result.stdout.split()


def _tracked_dirty_paths(repo: Path) -> list[str]:
    """Return paths with uncommitted *tracked* changes (untracked excluded).

    ``git status --porcelain`` prefixes each entry with a two-char status
    code; an untracked path is ``"?? "``.  A fast-forward ``git pull
    --ff-only`` and ``pip install -e`` never clobber untracked files, so
    they must NOT block the update (#924) — only tracked modifications a
    fast-forward could actually conflict with do.
    """
    lines = _git(repo, "status", "--porcelain").stdout.splitlines()
    return [line[3:] for line in lines if line and not line.startswith("??")]


def _has_upstream(repo: Path) -> bool:
    result = _git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        expected_codes=None,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _check_origin(name: str, repo: Path, *, is_primary: bool) -> RepoUpdate | None:
    del is_primary  # origin presence is not primary-clone-sensitive
    if _has_origin_remote(repo):
        return None
    return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no 'origin' remote configured")


def _check_fetch(name: str, repo: Path, *, is_primary: bool) -> RepoUpdate | None:
    del is_primary  # a fetch failure is already a hard FAILED for every repo
    fetch = _git(repo, "fetch", "origin", expected_codes=None)
    if fetch.returncode == 0:
        return None
    return RepoUpdate(name, UpdateStatus.FAILED, reason=f"git fetch failed: {fetch.stderr.strip()}")


def _warn_primary_off_default(name: str, repo: Path, current: str, default_branch: str | None) -> None:
    """Emit a prominent, un-missable block when the primary clone can't sync.

    The primary/running clone parked off its default branch (or with no
    upstream) means the editable ``t3`` the agent is running silently diverges
    from origin/main — a real currency hazard (#2134). Mirror the loud,
    multi-line WARNING shape of :func:`_check_clean` rather than a quiet SKIP
    line, naming the current branch and the one-line fix.
    """
    target = default_branch or "main"
    typer.echo("")
    typer.echo(f"!! WARNING: {name} is on branch {current!r}, not its default branch {target!r} — cannot sync.")
    typer.echo(f"!! The running editable t3 from {repo} will stay STALE behind origin until this is resolved.")
    typer.echo(f"!! Fix: git switch {target} && git pull --ff-only")
    typer.echo("")


def _check_default_branch(name: str, repo: Path, *, is_primary: bool) -> RepoUpdate | None:
    default_branch = _default_branch(repo)
    if default_branch is None:
        return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no origin/HEAD (no remote / no upstream)")
    current = _current_branch(repo)
    if not _has_upstream(repo):
        if is_primary:
            _warn_primary_off_default(name, repo, current, default_branch)
            return RepoUpdate(
                name,
                UpdateStatus.FAILED,
                reason=(
                    f"on branch {current!r} with no upstream — running t3 is STALE; "
                    f"`git switch {default_branch} && git pull --ff-only`"
                ),
            )
        return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no upstream tracking branch")
    if current != default_branch:
        if is_primary:
            _warn_primary_off_default(name, repo, current, default_branch)
            return RepoUpdate(
                name,
                UpdateStatus.FAILED,
                reason=(
                    f"on branch {current!r}, not default {default_branch!r} — running t3 is STALE; "
                    f"`git switch {default_branch} && git pull --ff-only`"
                ),
            )
        return RepoUpdate(
            name,
            UpdateStatus.SKIPPED,
            reason=f"on branch {current!r}, not default {default_branch!r}",
        )
    return None


def _check_clean(name: str, repo: Path, *, is_primary: bool) -> RepoUpdate | None:
    """Refuse a ff-pull only on uncommitted *tracked* changes — loudly.

    Untracked files (e.g. the loop's ``.loop-review-state.json`` runtime
    artifact) are tolerated: a fast-forward never touches them, so the
    update must proceed (#924).  When tracked changes do block the pull,
    this is NOT a silent ``SKIP`` line — it emits a prominent, multi-line
    WARNING so a stale running editable ``t3`` can never be invisible.  This
    is already loud for every repo, so it does not branch on *is_primary*.
    """
    del is_primary
    tracked = _tracked_dirty_paths(repo)
    if not tracked:
        return None
    listed = ", ".join(tracked)
    typer.echo("")
    typer.echo(f"!! WARNING: {name} has uncommitted TRACKED changes — refusing the fast-forward pull.")
    typer.echo(f"!! Changed tracked path(s): {listed}")
    typer.echo(f"!! The running editable t3 from {repo} will stay STALE behind origin until this is resolved.")
    typer.echo("!! Commit, stash, or revert the tracked change, then re-run `t3 update`.")
    typer.echo("")
    return RepoUpdate(
        name,
        UpdateStatus.SKIPPED,
        reason=f"uncommitted tracked changes ({listed}) — running t3 may be STALE; resolve and re-run `t3 update`",
    )


# Ordered safety gate: origin must exist before fetch, fetch before branch
# resolution (which needs origin/HEAD), branch before the tracked-dirty
# check.  Each guard *skips* (never clobbers) with a reason; only a failed
# fetch is a hard failure.  The order is load-bearing — do not reorder.
_PRECONDITIONS = (_check_origin, _check_fetch, _check_default_branch, _check_clean)


def _precondition_block(name: str, repo: Path, *, is_primary: bool) -> RepoUpdate | None:
    """Return the first terminal skip/fail outcome, or ``None`` if all clear."""
    for guard in _PRECONDITIONS:
        blocked = guard(name, repo, is_primary=is_primary)
        if blocked is not None:
            return blocked
    return None


def update_repo(name: str, repo: Path, *, is_primary: bool = False) -> RepoUpdate:
    """Fetch and fast-forward *repo* to its default branch, or skip safely.

    Never stashes, resets, or clobbers.  For an *overlay* repo a non-default
    branch or a missing upstream yields a soft :class:`UpdateStatus.SKIPPED`.
    For the *primary*/running clone (``is_primary=True``) those same states are
    a fail-loud currency hazard — the editable ``t3`` the agent runs would
    silently diverge from origin/main — so they yield
    :class:`UpdateStatus.FAILED` plus a prominent warning (#2134).  An
    untracked-only tree is NOT dirt — the ff-pull proceeds.  A failed ``git
    fetch`` / ``git pull`` always yields :class:`UpdateStatus.FAILED`.
    """
    blocked = _precondition_block(name, repo, is_primary=is_primary)
    if blocked is not None:
        return blocked

    old_sha = _short_sha(repo)
    pull = _git(repo, "pull", "--ff-only", expected_codes=None)
    if pull.returncode != 0:
        return RepoUpdate(name, UpdateStatus.FAILED, reason=f"git pull --ff-only failed: {pull.stderr.strip()}")

    new_sha = _short_sha(repo)
    if new_sha == old_sha:
        return RepoUpdate(name, UpdateStatus.UP_TO_DATE)
    advanced = _commit_count(repo, old_sha, new_sha)
    return RepoUpdate(name, UpdateStatus.UPDATED, old_sha=old_sha, new_sha=new_sha, advanced=advanced)


def _running_clone() -> Path | None:
    """The git work-tree the *running* interpreter imports ``teatree`` from.

    Resolved from ``teatree.__file__`` — independent of cwd/``T3_REPO``. A
    stale editable ``.pth`` anchored to a worktree makes this differ from the
    configured main clone, which is exactly the silent-isolation case the
    currency gate must catch (#1507).
    """
    import teatree  # noqa: PLC0415

    pkg = teatree.__file__
    if pkg is None:
        return None
    return _git_toplevel(Path(pkg).resolve().parent)


def _collect_repos() -> list[tuple[str, Path]]:
    """Discover teatree core, the running clone, and every registered overlay repo.

    Core is resolved via the same ``T3_REPO``/cwd logic ``t3 setup`` uses.
    The *running* clone — the work-tree the interpreter actually imports
    ``teatree`` from — is collected separately so the clone-currency gate
    audits the code the process runs, not just the configured main clone: a
    worktree-anchored editable install would otherwise sail past the #948 gate
    (#1507). Overlays come from ``discover_overlays()`` (the
    ``teatree.overlays`` entry points merged with ``[overlays.*]`` TOML
    config); each entry's ``project_path`` is walked up to its containing git
    work tree.
    """
    from teatree.cli.setup import _find_main_clone  # noqa: PLC0415
    from teatree.config import discover_overlays  # noqa: PLC0415

    repos: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    core = _find_main_clone()
    if core is not None:
        resolved = core.resolve()
        repos.append((_CORE_REPO_NAME, resolved))
        seen.add(resolved)

    running = _running_clone()
    if running is not None and running not in seen:
        seen.add(running)
        repos.append((_RUNNING_REPO_NAME, running))

    for entry in discover_overlays():
        if entry.project_path is None:
            continue
        repo = _git_toplevel(entry.project_path.expanduser())
        if repo is None or repo in seen:
            continue
        seen.add(repo)
        repos.append((entry.name, repo))

    return repos


def _git_toplevel(path: Path) -> Path | None:
    """Return the git work-tree root containing *path*, or None if not a repo."""
    if not path.is_dir():
        return None
    result = _git(path, "rev-parse", "--show-toplevel", expected_codes=None)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _reinstall_and_resetup(updated: list[RepoUpdate]) -> None:
    """Reinstall editable installs whose source advanced, then re-run setup.

    Reinstalling re-anchors the running ``t3`` (and overlay code) on the new
    sources; ``t3 setup`` afterwards re-syncs skill symlinks/config.  Both run
    through the audited subprocess wrapper; failures are surfaced but do not by
    themselves fail the run — the per-repo git outcome already did its job.
    """
    if not any(r.status is UpdateStatus.UPDATED for r in updated):
        typer.echo("No repo advanced — skipping reinstall + setup.")
        return

    if not shutil.which("uv"):
        typer.echo("WARN  `uv` not on PATH — skipping editable reinstall.")
    typer.echo("Reinstalling editable teatree + re-running `t3 setup` ...")
    result = reinstall_running_editable()
    if result.reinstalled:
        typer.echo("OK    Reinstalled teatree.")
    if result.ok:
        typer.echo("OK    `t3 setup` complete.")
    else:
        typer.echo(f"WARN  reinstall/setup reported a problem: {result.error}")


@update_app.callback()
def run(ctx: typer.Context) -> None:
    """Update teatree core + registered overlays (ff-only) and re-run setup.

    Idempotent and safe to re-run.  Skips (never clobbers) a dirty tree, a
    feature-branch checkout, or a missing upstream.  Exits non-zero only when
    a repo update hard-fails — not when one is skipped.
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_update()


def _run_update() -> None:
    """The actual update flow, factored out so the callback stays a thin shell."""
    repos = _collect_repos()
    if not repos:
        typer.echo("ERROR No teatree core or overlay repos found to update.")
        raise typer.Exit(code=1)

    results: list[RepoUpdate] = []
    for name, path in repos:
        typer.echo(f"Updating {name} ({path}) ...")
        results.append(update_repo(name, path, is_primary=name in _PRIMARY_REPO_NAMES))

    _reinstall_and_resetup(results)
    # Probe-gated and decoupled from the per-run UPDATED flag (#929): an
    # interrupted prior run or an out-of-band ff-pull leaves the SHA
    # current with a stale self-DB; this still migrates it.
    self_db_unmigrated = ensure_self_db_migrated()

    typer.echo("")
    typer.echo("Summary:")
    for result in results:
        typer.echo(f"  {result.summary_line}")

    if self_db_unmigrated or any(result.is_error for result in results):
        raise typer.Exit(code=1)
