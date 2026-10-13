"""README staleness advisory based on feature commits since its last edit."""

from pathlib import Path

import typer

from teatree.paths import CODE_REPO_ROOT
from teatree.utils.git_run import run_strict

STALE_FEATURE_COUNT = 5


def readme_feature_merges_since_update(repo: Path) -> int:
    last = run_strict(repo=str(repo), args=["log", "-1", "--format=%H", "HEAD", "--", "README.md"])
    if not last:
        msg = "README.md has no tracked commit"
        raise ValueError(msg)
    log = run_strict(repo=str(repo), args=["log", "--first-parent", "--format=%s", f"{last}..HEAD"])
    return sum(subject.startswith(("feat:", "feat(")) for subject in log.splitlines())


def _check_readme_staleness() -> bool:
    try:
        count = readme_feature_merges_since_update(CODE_REPO_ROOT)
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"WARN  README freshness could not be checked: {exc}")
        return False
    if count < STALE_FEATURE_COUNT:
        return True
    typer.echo(f"WARN  README has not changed across {count} feature commits; review its capability prose.")
    return False
