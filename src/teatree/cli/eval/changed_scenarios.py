"""``t3 eval changed-scenarios`` — the reusable selective-PR scenario selector.

Reads a PR's changed file paths from STDIN (one repo-relative POSIX path per
line — exactly what ``git diff --name-only`` emits) and prints the ``name`` of
each discovered scenario whose source YAML the PR touched, one per line. The
shared core lives in :mod:`teatree.eval.changed_scenarios`; the host's
``scripts/eval/scenarios_for_changed.py`` shim and this overlay-facing CLI both
delegate to it, so the selective-PR eval workflow is the same logic everywhere.

Exit 0 when at least one scenario matched; exit ``--skip-code`` (default 1) when
nothing matched, so the caller's eval job skips cleanly with no API spend.
"""

import sys

import typer

from teatree.eval.changed_scenarios import select_changed_scenario_names
from teatree.utils.django_bootstrap import ensure_django


def changed_scenarios(
    skip_code: int = typer.Option(1, "--skip-code", help="Exit code when no scenario file changed."),
) -> None:
    """Print the scenario names a PR's STDIN diff touched; exit --skip-code when none."""
    ensure_django()
    names = select_changed_scenario_names(sys.stdin)
    if not names:
        raise SystemExit(skip_code)
    for name in names:
        typer.echo(name)
