r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
script is that selector: it reads the PR's changed file paths from STDIN (one
repo-relative POSIX path per line, exactly what ``git diff --name-only`` emits),
discovers every spec via ``discover_specs()``, and prints the ``name`` of each
spec whose ``source_path`` — normalized to repo-relative — equals one of the
changed paths. One YAML file may define several scenarios sharing a
``source_path``, so editing that file selects all of them.

Exit 0 when at least one scenario matched (its names were printed) so the eval
runs; exit ``--skip-code`` (default 1) when nothing matched (no scenario file
changed) so the ``eval-pr`` workflow's eval job is skipped cleanly, no API spend.

``source_path`` is absolute (``discover_specs`` globs an absolute catalog dir),
while the diff gives repo-relative paths; both are normalized to repo-relative
before comparing so the match is representation-independent.
"""

import argparse
import sys
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when no scenario file changed.")
    args = parser.parse_args(argv)
    names = names_for_changed(sys.stdin, discover_specs(), REPO_ROOT)
    if not names:
        return args.skip_code
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
