"""t3 update — sync teatree core + registered overlays to their default branch.

Updating is a *mutating, network, version-changing* operation, deliberately
kept separate from the idempotent ``t3 setup`` bootstrap.  ``t3 update`` reuses
the setup/reinstall step at the end; ``t3 setup`` never reaches into ``update``.

For teatree core (``$T3_REPO``) and every registered overlay repo, this:

1. ``git fetch`` the origin.
2. Resolves the default branch from ``origin/HEAD``.
3. Skips a non-default-branch / no-upstream checkout, and a
    tracked-dirty tree (loudly). Untracked-only files do not block it.
4. Otherwise ``git pull --ff-only`` — fast-forward only, never merge/rebase.
5. Reinstalls advanced editable installs, applies pending self-DB migrations (non-destructive), then runs ``t3 setup``.
6. Prints a per-repo summary; exits non-zero only on a hard failure (not a skip).

This module is a top-level Typer group reached through the typer runner
directly (sibling of ``t3 setup`` / ``t3 doctor``), so it raises
``typer.Exit(code=N)`` — *not* ``SystemExit`` (which is for ``TyperCommand``
groups reached via Django ``call_command``; see ``skills/teatree`` § "CLI exit
codes").  Precedent: ``cli/setup.py`` ``_validate_repo`` → ``raise typer.Exit``.
"""

import enum
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.run import CompletedProcess, run_allowed_to_fail

update_app = typer.Typer(
    help="Sync teatree core and registered overlays to their default branch.",
    invoke_without_command=True,
)


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

    @property
    def is_error(self) -> bool:
        return self.status is UpdateStatus.FAILED

    @property
    def summary_line(self) -> str:
        if self.status is UpdateStatus.UPDATED:
            return f"OK    {self.name}: updated {self.old_sha} -> {self.new_sha}"
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


def _check_origin(name: str, repo: Path) -> RepoUpdate | None:
    if _has_origin_remote(repo):
        return None
    return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no 'origin' remote configured")


def _check_fetch(name: str, repo: Path) -> RepoUpdate | None:
    fetch = _git(repo, "fetch", "origin", expected_codes=None)
    if fetch.returncode == 0:
        return None
    return RepoUpdate(name, UpdateStatus.FAILED, reason=f"git fetch failed: {fetch.stderr.strip()}")


def _check_default_branch(name: str, repo: Path) -> RepoUpdate | None:
    default_branch = _default_branch(repo)
    if default_branch is None:
        return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no origin/HEAD (no remote / no upstream)")
    if not _has_upstream(repo):
        return RepoUpdate(name, UpdateStatus.SKIPPED, reason="no upstream tracking branch")
    current = _current_branch(repo)
    if current != default_branch:
        return RepoUpdate(
            name,
            UpdateStatus.SKIPPED,
            reason=f"on branch {current!r}, not default {default_branch!r}",
        )
    return None


def _check_clean(name: str, repo: Path) -> RepoUpdate | None:
    """Refuse a ff-pull only on uncommitted *tracked* changes — loudly.

    Untracked files (e.g. the loop's ``.loop-review-state.json`` runtime
    artifact) are tolerated: a fast-forward never touches them, so the
    update must proceed (#924).  When tracked changes do block the pull,
    this is NOT a silent ``SKIP`` line — it emits a prominent, multi-line
    WARNING so a stale running editable ``t3`` can never be invisible.
    """
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


def _precondition_block(name: str, repo: Path) -> RepoUpdate | None:
    """Return the first terminal skip/fail outcome, or ``None`` if all clear."""
    for guard in _PRECONDITIONS:
        blocked = guard(name, repo)
        if blocked is not None:
            return blocked
    return None


def update_repo(name: str, repo: Path) -> RepoUpdate:
    """Fetch and fast-forward *repo* to its default branch, or skip safely.

    Never stashes, resets, or clobbers: a tracked-dirty tree (warned
    loudly), a non-default branch, or a missing upstream each yield
    :class:`UpdateStatus.SKIPPED` with a reason.  An untracked-only tree
    is NOT dirt — the ff-pull proceeds.  A failed ``git fetch`` / ``git
    pull`` yields :class:`UpdateStatus.FAILED`.
    """
    blocked = _precondition_block(name, repo)
    if blocked is not None:
        return blocked

    old_sha = _short_sha(repo)
    pull = _git(repo, "pull", "--ff-only", expected_codes=None)
    if pull.returncode != 0:
        return RepoUpdate(name, UpdateStatus.FAILED, reason=f"git pull --ff-only failed: {pull.stderr.strip()}")

    new_sha = _short_sha(repo)
    if new_sha == old_sha:
        return RepoUpdate(name, UpdateStatus.UP_TO_DATE)
    return RepoUpdate(name, UpdateStatus.UPDATED, old_sha=old_sha, new_sha=new_sha)


def _collect_repos() -> list[tuple[str, Path]]:
    """Discover teatree core and every registered overlay repo.

    Core is resolved via the same ``T3_REPO``/cwd logic ``t3 setup`` uses.
    Overlays come from ``discover_overlays()`` (the ``teatree.overlays`` entry
    points merged with ``[overlays.*]`` TOML config); each entry's
    ``project_path`` is walked up to its containing git work tree.
    """
    from teatree.cli.setup import _find_main_clone  # noqa: PLC0415
    from teatree.config import discover_overlays  # noqa: PLC0415

    repos: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    core = _find_main_clone()
    if core is not None:
        resolved = core.resolve()
        repos.append(("teatree", resolved))
        seen.add(resolved)

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


def _migrate_self_db(source: Path) -> None:
    """Apply pending teatree self-DB migrations non-destructively.

    A teatree git-pull can land new migrations; ``t3 update`` must apply
    them or the sanctioned merge path breaks against the now-stale
    self-DB. Runs the same ``uv --directory <clone> run python manage.py
    migrate --no-input`` wrapper ``resetdb`` uses internally — WITHOUT
    the destructive DB drop, so live ticket/session/lease state is
    preserved. This is the first-class t3 alternative to the destructive
    ``resetdb`` and the hook-discouraged raw ``manage.py migrate``. A
    failure warns (the per-repo git outcome already did its job); it
    never raises.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        typer.echo("WARN  `uv` not on PATH — skipping self-DB migration.")
        return
    typer.echo("Applying teatree self-DB migrations (non-destructive) ...")
    result = run_allowed_to_fail(
        [uv_bin, "--directory", str(source), "run", "python", "manage.py", "migrate", "--no-input"],
        expected_codes=None,
    )
    if result.returncode != 0:
        typer.echo(f"WARN  self-DB migration failed: {result.stderr.strip() or result.stdout.strip()}")
    else:
        typer.echo("OK    self-DB migrations applied.")


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

    uv_bin = shutil.which("uv")
    if uv_bin:
        from teatree.cli.setup import _current_editable_source  # noqa: PLC0415

        source = _current_editable_source(uv_bin)
        if source is not None and source.is_dir():
            typer.echo(f"Reinstalling editable teatree from {source} ...")
            result = run_allowed_to_fail(
                [uv_bin, "tool", "install", "--editable", str(source), "--reinstall"],
                expected_codes=None,
            )
            if result.returncode != 0:
                typer.echo(f"WARN  Reinstall failed: {result.stderr.strip()}")
            else:
                typer.echo("OK    Reinstalled teatree.")
            _migrate_self_db(source)
    else:
        typer.echo("WARN  `uv` not on PATH — skipping editable reinstall.")

    t3_bin = shutil.which("t3") or sys.argv[0]
    typer.echo("Re-running `t3 setup` ...")
    result = run_allowed_to_fail([t3_bin, "setup"], expected_codes=None)
    typer.echo(result.stdout.rstrip() if result.stdout.strip() else "")
    if result.returncode != 0:
        typer.echo(f"WARN  `t3 setup` reported a problem: {result.stderr.strip()}")


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
        results.append(update_repo(name, path))

    _reinstall_and_resetup(results)

    typer.echo("")
    typer.echo("Summary:")
    for result in results:
        typer.echo(f"  {result.summary_line}")

    if any(result.is_error for result in results):
        raise typer.Exit(code=1)
