"""Pre-investigation stale-clone hard-fail gate (#948).

A bug-investigation sub-agent that begins root-causing against a clone
many commits behind ``origin/<default>`` forms an initially-wrong
root-cause hypothesis on phantom symptoms. :mod:`teatree.core.branch_currency`
(#940) covers branch-currency *before cold review/ship* (the PR-branch
exit-point). This module covers the earlier point: **before any bug
investigation reads repo files**.

The gate mirrors :mod:`teatree.core.gates.schema_guard` (#869). For each
in-scope clone it runs ``git fetch origin``, then asserts
``origin/<default>`` is an ancestor of ``HEAD``. If a clone is behind it
raises :class:`StaleCloneError` with an actionable message — not a
warning, a deterministic refusal — quoting the remediation command (the
canonical ``t3 update`` and the in-repo fallback) so the sub-agent
cannot proceed against stale code.

Distinct from :mod:`teatree.core.branch_currency` (#940): this is the
*entry-point* gate (before reading any file for investigation); #940 is
the *exit-point* gate (before cold review/ship on a feature branch).
"""

from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.run import run_allowed_to_fail

_REMEDIATION = (
    "Sync the clone before investigating:\n"
    "  t3 update     # canonical — fetches every registered clone\n"
    "or, in the affected clone:\n"
    "  git -C <clone> pull --ff-only\n"
    "`t3 doctor check` flags this gap at session start."
)


@dataclass(frozen=True, slots=True)
class CloneStaleness:
    """One stale-clone finding: a clone behind its default-branch tip."""

    name: str
    path: Path
    default_branch: str
    behind: int


class StaleCloneError(RuntimeError):
    """Raised when one or more in-scope clones are behind ``origin/<default>``.

    Carries the per-clone staleness so the caller can render an
    actionable message instead of a silent wrong-root-cause hypothesis.
    """


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Return ``(returncode, stdout)`` for ``git -C <repo> <args>``.

    Uses :func:`run_allowed_to_fail` so a non-zero exit (no remote, bad
    ref, network failure on fetch) yields a probe-style result rather
    than crashing the gate — the per-check caller decides how to react.
    """
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), *args],
        expected_codes=None,
    )
    return result.returncode, result.stdout.strip()


def _default_branch(repo: Path) -> str | None:
    """Resolve the default branch from ``origin/HEAD`` (e.g. ``main``).

    Returns ``None`` when ``origin/HEAD`` is unset — the clone has no
    discoverable default branch and must be skipped (same posture as
    ``t3 update``'s ``_check_default_branch``).
    """
    rc, out = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc != 0 or not out:
        return None
    # "refs/remotes/origin/main" -> "main"
    return out.rsplit("/", 1)[-1]


def _has_origin_remote(repo: Path) -> bool:
    rc, out = _git(repo, "remote")
    return rc == 0 and "origin" in out.split()


def _fetch_origin(repo: Path) -> bool:
    """Fetch ``origin`` for *repo*. Returns ``True`` on success.

    A failed fetch (offline, auth) is treated as inconclusive — the
    gate must not block the user when the network is down. The caller
    surfaces this as a WARN, matching the schema_guard "DB offline is
    valid" posture.
    """
    rc, _ = _git(repo, "fetch", "origin")
    return rc == 0


def _commits_behind(repo: Path, default_branch: str) -> int:
    """Count commits on ``origin/<default>`` not reachable from ``HEAD``.

    Zero means ``origin/<default>`` is an ancestor of ``HEAD`` — the
    clone has caught up. Positive means the clone is behind by that
    many commits.
    """
    rc, out = _git(repo, "rev-list", "--count", f"HEAD..origin/{default_branch}")
    if rc != 0 or not out:
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


def clones_behind_default(repos: list[tuple[str, Path]]) -> list[CloneStaleness]:
    """Return one :class:`CloneStaleness` for every clone behind ``origin/<default>``.

    Empty list ⇒ every in-scope clone is current. Each repo is fetched
    first so the comparison reflects the remote's real HEAD, not a
    cached refs snapshot.

    Repos without an ``origin`` remote or without ``origin/HEAD`` are
    skipped (no discoverable default branch). A failed fetch yields a
    skip — the caller's WARN surface decides whether to block.
    """
    findings: list[CloneStaleness] = []
    for name, path in repos:
        if not path.is_dir() or not _has_origin_remote(path):
            continue
        if not _fetch_origin(path):
            continue
        default = _default_branch(path)
        if default is None:
            continue
        behind = _commits_behind(path, default)
        if behind > 0:
            findings.append(
                CloneStaleness(name=name, path=path, default_branch=default, behind=behind),
            )
    return findings


def require_current_clones(repos: list[tuple[str, Path]]) -> None:
    """Fail closed if any in-scope clone is behind its ``origin/<default>``.

    Called as a pre-flight by the pre-investigation path so an
    investigation sub-agent can never form a root-cause hypothesis
    against stale code.
    """
    stale = clones_behind_default(repos)
    if not stale:
        return
    lines = [
        f"refusing to investigate stale code; sync first — {len(stale)} clone(s) behind origin/<default>:",
        *(
            f"  - {finding.name} ({finding.path}): {finding.behind} commit(s) behind origin/{finding.default_branch}"
            for finding in stale
        ),
        "",
        _REMEDIATION,
    ]
    raise StaleCloneError("\n".join(lines))


def doctor_check_clone_currency(repos: list[tuple[str, Path]]) -> bool:
    """``t3 doctor`` surface for the pre-investigation clone-currency gate (#948).

    Returns ``True`` (check passed) when every discovered clone is
    current. Returns ``False`` with a ``FAIL`` line per stale clone so
    the gap surfaces at session start instead of mid-investigation.

    The caller resolves *repos* — the doctor CLI uses
    :func:`teatree.cli.update._collect_repos`. The core module cannot
    import from ``teatree.cli`` (tach module boundary), so dependency
    injection keeps the layering clean.
    """
    stale = clones_behind_default(repos)
    if not stale:
        return True
    for finding in stale:
        typer.echo(
            f"FAIL  {finding.name} clone at {finding.path} is "
            f"{finding.behind} commit(s) behind origin/{finding.default_branch} — "
            f"run `t3 update` (or `git -C {finding.path} pull --ff-only`) before any "
            f"investigation.",
        )
    return False
