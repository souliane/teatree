"""README staleness advisory based on feature commits since its last prose edit."""

import re
from itertools import pairwise
from pathlib import Path

import typer

from teatree.paths import CODE_REPO_ROOT
from teatree.utils.git_run import run_strict

STALE_FEATURE_COUNT = 5
_GENERATED_BLOCK = re.compile(r"<!-- BEGIN (SKILLS|LOOPS) -->.*?<!-- END \1 -->", re.DOTALL)


def _readme_prose(repo: Path, revision: str) -> str:
    readme = run_strict(repo=str(repo), args=["show", f"{revision}:README.md"])
    return _GENERATED_BLOCK.sub("", readme)


def readme_feature_merges_since_update(repo: Path) -> int:
    revisions = run_strict(
        repo=str(repo), args=["log", "--first-parent", "--format=%H", "HEAD", "--", "README.md"]
    ).splitlines()
    if not revisions:
        msg = "README.md has no tracked commit"
        raise ValueError(msg)
    last_prose_edit = revisions[-1]
    newer_prose = _readme_prose(repo, revisions[0])
    for newer, older in pairwise(revisions):
        older_prose = _readme_prose(repo, older)
        if newer_prose != older_prose:
            last_prose_edit = newer
            break
        newer_prose = older_prose
    log = run_strict(repo=str(repo), args=["log", "--first-parent", "--format=%s", f"{last_prose_edit}..HEAD"])
    return sum(subject.startswith(("feat:", "feat(")) for subject in log.splitlines())


def _check_readme_staleness() -> bool:
    if not (CODE_REPO_ROOT / ".git").exists():
        return True
    try:
        count = readme_feature_merges_since_update(CODE_REPO_ROOT)
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"WARN  README freshness could not be checked: {exc}")
        return False
    if count < STALE_FEATURE_COUNT:
        return True
    typer.echo(f"WARN  README has not changed across {count} feature commits; review its capability prose.")
    return False
