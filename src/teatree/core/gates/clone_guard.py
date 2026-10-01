"""Session-start clone-currency check (#948).

A bug-investigation sub-agent that begins root-causing against a clone
many commits behind ``origin/<default>`` forms an initially-wrong
root-cause hypothesis on phantom symptoms. ``t3 doctor check`` therefore
fetches every registered clone at session start and FAILs — gating the
doctor's exit code — on each one whose ``origin/<default>`` is not an
ancestor of ``HEAD``, quoting the remediation (``t3 update``, or
``git pull --ff-only`` in the clone).

It is a surfacing check, not an investigation-time refusal: nothing calls
it before an agent reads a file. A clone whose currency cannot be READ —
the fetch failed, or ``origin/<default>`` no longer resolves — is reported
as a WARN naming why, never as current, and does not gate: a network
outage must not fail the doctor (the ``schema_guard`` "DB offline" posture).

Distinct from :mod:`teatree.core.worktree.branch_currency` (#940), the
PR-branch check before cold review/ship.
"""

from dataclasses import dataclass, field
from pathlib import Path

import typer

from teatree.utils.run import run_allowed_to_fail


@dataclass(frozen=True, slots=True)
class CloneStaleness:
    """One stale-clone finding: a clone behind its default-branch tip."""

    name: str
    path: Path
    default_branch: str
    behind: int


@dataclass(frozen=True, slots=True)
class UnverifiedClone:
    """A clone whose currency could not be read, and why — UNKNOWN, not current."""

    name: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class CloneCurrency:
    """What one survey established: the stale clones, and the ones it could not judge."""

    stale: list[CloneStaleness] = field(default_factory=list)
    unverified: list[UnverifiedClone] = field(default_factory=list)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Return ``(returncode, stdout)`` for ``git -C <repo> <args>``."""
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), *args],
        expected_codes=None,
    )
    return result.returncode, result.stdout.strip()


def _default_branch(repo: Path) -> str | None:
    """Resolve the default branch from ``origin/HEAD`` (e.g. ``main``).

    Returns ``None`` when ``origin/HEAD`` is unset — the clone has no
    discoverable default branch and is skipped (same posture as
    ``t3 update``'s ``_check_default_branch``).
    """
    rc, out = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc != 0 or not out:
        return None
    return out.rsplit("/", 1)[-1]


def _has_origin_remote(repo: Path) -> bool:
    rc, out = _git(repo, "remote")
    return rc == 0 and "origin" in out.split()


def _commits_behind(repo: Path, default_branch: str) -> int | None:
    """Commits on ``origin/<default>`` not reachable from ``HEAD``; ``None`` when unreadable."""
    rc, out = _git(repo, "rev-list", "--count", f"HEAD..origin/{default_branch}")
    if rc != 0:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def clone_currency(repos: list[tuple[str, Path]]) -> CloneCurrency:
    """Fetch each clone, then judge it stale, current, or unverified.

    Repos that are not a directory, have no ``origin`` remote, or no
    ``origin/HEAD`` are skipped: none has a default branch to compare against.
    """
    survey = CloneCurrency()
    for name, path in repos:
        if not path.is_dir() or not _has_origin_remote(path):
            continue
        if _git(path, "fetch", "origin")[0] != 0:
            survey.unverified.append(
                UnverifiedClone(name, path, "git fetch origin failed — offline or unauthenticated")
            )
            continue
        default = _default_branch(path)
        if default is None:
            continue
        behind = _commits_behind(path, default)
        if behind is None:
            reason = f"origin/{default} does not resolve — origin/HEAD may name a renamed branch"
            survey.unverified.append(UnverifiedClone(name, path, reason))
        elif behind > 0:
            survey.stale.append(CloneStaleness(name=name, path=path, default_branch=default, behind=behind))
    return survey


def doctor_check_clone_currency(repos: list[tuple[str, Path]]) -> bool:
    """``t3 doctor`` surface: ``False`` with a FAIL line per stale clone, a WARN per unverified one.

    The caller resolves *repos* — the doctor CLI uses
    :func:`teatree.cli.update._collect_repos`. The core module cannot
    import from ``teatree.cli`` (tach module boundary), so dependency
    injection keeps the layering clean.
    """
    survey = clone_currency(repos)
    for unverified in survey.unverified:
        typer.echo(
            f"WARN  {unverified.name} clone at {unverified.path}: currency not verified — {unverified.reason}.",
        )
    for finding in survey.stale:
        typer.echo(
            f"FAIL  {finding.name} clone at {finding.path} is "
            f"{finding.behind} commit(s) behind origin/{finding.default_branch} — "
            f"run `t3 update` (or `git -C {finding.path} pull --ff-only`) before any "
            f"investigation.",
        )
    return not survey.stale
