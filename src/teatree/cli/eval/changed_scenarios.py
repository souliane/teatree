"""``t3 eval changed-scenarios`` — the reusable selective-PR scenario selector.

Reads a PR's changed file paths from STDIN (one repo-relative POSIX path per
line — exactly what ``git diff --name-only`` emits) and prints the ``name`` of
each discovered scenario whose source YAML the PR touched, one per line. The
shared core lives in :mod:`teatree.eval.changed_scenarios`; the host's
``scripts/eval/scenarios_for_changed.py`` shim and this overlay-facing CLI both
delegate to it, so the selective-PR eval workflow is the same logic everywhere.

A consuming overlay's diff paths are relative to ITS repo root and it owns only
ITS scenarios, so ``--repo-root`` sets what the diff paths are relative to
(default: teatree's own root, for back-compat) and ``--scenarios-dir`` filters
the discovered union catalog to the specs under that directory. Without the
filter a core-scenario edit would drag scenarios that are not the consumer's
into its lane; without the right root the diff matches nothing and the lane
reads that as a clean skip that never ran an eval. ``--require-specs`` closes
that quiet-skip hole: it fails loud (exit 2) when the filtered catalog is empty,
because "the catalog is empty" and "nothing changed" are different answers that
skip identically today.

Exit 0 when at least one scenario matched; exit ``--skip-code`` (default 1) when
nothing matched, so the caller's eval job skips cleanly with no API spend.
"""

import sys
from pathlib import Path

import typer

from teatree.eval.changed_scenarios import REPO_ROOT, select_changed_scenarios, specs_under
from teatree.eval.discovery import discover_specs
from teatree.utils.django_bootstrap import ensure_django

_EMPTY_CATALOG_EXIT = 2


def changed_scenarios(
    skip_code: int = typer.Option(1, "--skip-code", help="Exit code when no scenario file changed."),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Root the STDIN diff paths are relative to (default: teatree's own repo root).",
    ),
    scenarios_dir: Path | None = typer.Option(
        None,
        "--scenarios-dir",
        help="Filter the discovered catalog to specs under this directory (default: the whole union catalog).",
    ),
    require_specs: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--require-specs",
        help="Fail loud (exit 2) when the filtered catalog is empty, instead of skipping like 'nothing changed'.",
    ),
) -> None:
    """Print the scenario names a PR's STDIN diff touched; exit --skip-code when none."""
    ensure_django()
    specs = discover_specs()
    if scenarios_dir is not None:
        specs = specs_under(specs, scenarios_dir)
    if require_specs and not specs:
        typer.echo(
            f"no eval scenarios discovered under {scenarios_dir} — the filtered catalog is empty. "
            "This is NOT 'nothing changed'; refusing to skip (--require-specs).",
            err=True,
        )
        raise SystemExit(_EMPTY_CATALOG_EXIT)
    selection = select_changed_scenarios(sys.stdin, repo_root=repo_root or REPO_ROOT, specs=specs)
    # Surface the cap when it bites (#2737) so a corpus-wide PR's truncated coverage is
    # visible in the CI log — the deferred scenarios run in the weekly sharded lane.
    if note := selection.truncation_note():
        typer.echo(note, err=True)
    if not selection.names:
        raise SystemExit(skip_code)
    for name in selection.names:
        typer.echo(name)
