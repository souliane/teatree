r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
module is that selector: given the PR's changed file paths (one repo-relative
POSIX path per line, exactly what ``git diff --name-only`` emits), it discovers
every spec via :func:`discover_specs` and returns the ``name`` of each spec whose
``source_path`` — normalized to repo-relative — equals one of the changed paths.
One YAML file may define several scenarios sharing a ``source_path``, so editing
that file selects all of them.

``source_path`` is absolute (``discover_specs`` globs an absolute catalog dir),
while the diff gives repo-relative paths; both are normalized to repo-relative
before comparing so the match is representation-independent.

This is the shared core behind both ``t3 eval changed-scenarios`` (the reusable
overlay-facing CLI) and ``scripts/eval/scenarios_for_changed.py`` (the thin
script the host workflow shells out to). Both delegate here; the logic lives once.
"""

from collections.abc import Iterable
from pathlib import Path

from teatree.eval.discovery import SCENARIOS_DIR, discover_specs
from teatree.eval.models import EvalSpec

REPO_ROOT = SCENARIOS_DIR.parents[1]


def _relative_to_root(path: Path, repo_root: Path) -> str:
    candidate = path if path.is_absolute() else repo_root / path
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def names_for_changed(changed: Iterable[str], specs: Iterable[EvalSpec], repo_root: Path) -> list[str]:
    wanted = {_relative_to_root(Path(line.strip()), repo_root) for line in changed if line.strip()}
    matched = {spec.name for spec in specs if _relative_to_root(spec.source_path, repo_root) in wanted}
    return sorted(matched)


def select_changed_scenario_names(changed: Iterable[str]) -> list[str]:
    return names_for_changed(changed, discover_specs(), REPO_ROOT)
