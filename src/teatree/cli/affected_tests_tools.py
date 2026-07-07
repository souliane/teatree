"""``t3 tool affected-tests`` — safety-biased incremental test selection (#113).

Registers onto the shared ``tool_app`` (side-effect import from ``cli/__init__``,
mirroring ``push_gate_tools`` / ``test_path_mirror_tools``). The selection logic lives
in :mod:`teatree.quality.affected_tests`; this module is the thin CLI surface — it
builds the selection for the current diff and prints it as a human report, JSON,
``--pytest-args`` (for ``xargs uv run pytest``), or an ``--explain`` chain.

Informational only: it never exits non-zero and never gates a push. The whole-tree
sharded run stays the merge/coverage gate.
"""

import json
from pathlib import Path
from typing import Any

import typer

from teatree.cli.tools import tool_app
from teatree.quality.affected_tests import Selection, build_selection


def _selection_as_dict(selection: Selection) -> dict[str, Any]:
    return {
        "full": selection.full,
        "reason": selection.reason,
        "create_db": selection.create_db,
        "pytest_args": selection.pytest_args(),
        "test_files": list(selection.test_files),
        "floor_dirs": list(selection.floor_dirs),
        "doctest_targets": list(selection.doctest_targets),
        "changed_src": list(selection.changed_src),
        "changed_tests": list(selection.changed_tests),
        "warnings": list(selection.warnings),
        "reasons": [{"test": r.test, "kind": r.kind, "chain": list(r.chain)} for r in selection.reasons],
    }


@tool_app.command("affected-tests")
def affected_tests_command(
    base: str = typer.Option("origin/main", "--base", help="Merge-base ref for the changed set."),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit the machine-readable selection."),
    pytest_args: bool = typer.Option(
        False, "--pytest-args", help="Emit the pytest positional args (for `xargs uv run pytest`)."
    ),
    explain: str | None = typer.Option(
        None, "--explain", help="Trace the selection chain for a test path, or 'all' for every selected test."
    ),
) -> None:
    """Select the pytest tests a diff affects — over-selecting, never under.

    Fast-feedback ONLY: the whole-tree sharded run stays the merge/coverage gate; this
    is opt-in local tooling, never wired into the pre-push gate. Any change the
    classifier cannot prove local (conftest/settings/migrations/data files/deletions/
    files outside the modelled roots) degrades to a whole-tree FULL run.
    """
    selection = build_selection(Path.cwd(), base_ref=base)

    if output_json:
        typer.echo(json.dumps(_selection_as_dict(selection), indent=2))
        return
    if pytest_args:
        typer.echo(" ".join(selection.pytest_args()))
        return
    if explain is not None:
        target = None if explain in {"", "all"} else explain
        for line in selection.explain(target):
            typer.echo(line)
        return

    typer.echo(selection.report())
    typer.echo(f"reason: {selection.reason}")
    for warning in selection.warnings:
        typer.echo(f"warning: {warning}")
