"""``t3 tool affected-tests`` — safety-biased incremental test selection (#113, #3672).

Registers onto the shared ``tool_app`` (side-effect import from ``cli/__init__``,
mirroring ``push_gate_tools`` / ``test_path_mirror_tools``). The selection logic lives
in :mod:`teatree.quality.affected_tests`; this module is the thin CLI surface — it
builds the selection for the current diff and prints it as a human report, JSON,
``--pytest-args`` (for ``uv run pytest``), or an ``--explain`` chain.

The impact engine is the tach pytest plugin; this tool decides FULL-vs-scoped, emits the
plugin invocation for a scoped run, and reports the escalation force-keep layer applied
over the plugin's deselection. Informational only: it never exits non-zero and never
gates a push. The whole-tree sharded run stays the merge/coverage gate.
"""

import json
from pathlib import Path
from typing import Any

import typer

from teatree.quality.affected_tests import Selection, build_selection


def _selection_as_dict(selection: Selection) -> dict[str, Any]:
    return {
        "full": selection.full,
        "reason": selection.reason,
        "create_db": selection.create_db,
        "base_ref": selection.base_ref,
        "pytest_args": selection.pytest_args(),
        "force_keep": list(selection.force_keep),
        "doctest_targets": list(selection.doctest_targets),
        "changed_src": list(selection.changed_src),
        "changed_tests": list(selection.changed_tests),
        "changed_docs": list(selection.changed_docs),
        "warnings": list(selection.warnings),
        "reasons": [{"test": r.test, "kind": r.kind, "chain": list(r.chain)} for r in selection.reasons],
    }


def affected_tests_command(
    base: str = typer.Option("origin/main", "--base", help="Merge-base ref for the changed set."),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit the machine-readable selection."),
    pytest_args: bool = typer.Option(
        False, "--pytest-args", help="Emit the pytest positional args (for `uv run pytest`)."
    ),
    explain: str | None = typer.Option(
        None, "--explain", help="Trace the force-keep reason for a test path, or 'all' for every force-kept test."
    ),
) -> None:
    """Select the pytest tests a diff affects — the tach plugin deselects, we force-keep.

    Fast-feedback ONLY: the whole-tree sharded run stays the merge/coverage gate; this
    is opt-in local tooling, never wired into the pre-push gate. Any change the
    classifier cannot prove local (conftest/settings/migrations/data files/deletions/
    files outside the modelled roots) degrades to a whole-tree FULL run with the plugin
    off.
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


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("affected-tests")(affected_tests_command)
