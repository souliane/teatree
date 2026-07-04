"""``t3 tool test-path-mirror`` — the test-files-mirror-src forward-guard.

Registers onto the shared ``tool_app`` (side-effect import from
``cli/__init__``, mirroring ``test_shape_tools`` / ``triage_tools``). The
analysis lives in :mod:`teatree.quality.test_path_mirror`; this module is the
thin CLI surface: it resolves the repo root, loads
``[tool.teatree.test_path_mirror]``, builds the report, prints it, and chooses
the exit code from the ratchet.

Exit codes: ``0`` when every live violation is grandfathered and no ledger entry
is stale; ``1`` when a live violation is NOT in the ledger (a new mis-pathed
file) or a ledger entry no longer violates (forced banking demands its removal).

``--update-baseline`` rewrites the committed ledger file to the exact live
violation set. The ratchet only moves down: an update that would ADD an entry
(a path not already grandfathered) is REFUSED unless ``--allow-regression`` is
also passed. Without that guard the gate is vacuous — a new mis-pathed file
could be "grandfathered away" silently. The escape exists for an intentional,
reviewed rise, but it must be an explicit, visible choice.
"""

import json
from pathlib import Path

import typer

from teatree.cli.tools import tool_app
from teatree.quality.test_path_mirror import Ledger, MirrorReport, build_report, find_violations, load_config


def _resolve_root(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _report_json(report: MirrorReport) -> str:
    return json.dumps(
        {
            "grandfathered_count": len(report.grandfathered),
            "live_count": report.live_count,
            "failed": report.failed,
            "unknown_violations": [v.path for v in report.unknown_violations],
            "stale_entries": list(report.stale_entries),
            "violations": [
                {
                    "path": v.path,
                    "imported_modules": list(v.imported_modules),
                    "expected_dirs": list(v.expected_dirs),
                }
                for v in report.violations
            ],
        },
        indent=2,
    )


def _print_report(report: MirrorReport) -> None:
    if not report.failed:
        typer.echo(
            f"test-path-mirror: {report.live_count} grandfathered violation(s), ledger exact "
            "(test files mirror src — ratchet holds)."
        )
        return
    if report.unknown_violations:
        typer.echo(f"test-path-mirror REGRESSION: {len(report.unknown_violations)} new mis-pathed test file(s):")
        typer.echo("")
        for line in report.summary_lines():
            typer.echo(line)
        typer.echo("")
        typer.echo(
            "A test file must mirror its src/teatree/<pkg>/... module path as tests/teatree_<pkg>/... . "
            "Move the new file, or (for a genuine multi-package contract test) add a "
            "`# test-path: cross-cutting` pragma."
        )
    if report.stale_entries:
        typer.echo(f"test-path-mirror STALE LEDGER: {len(report.stale_entries)} entry(ies) no longer violate:")
        typer.echo("")
        for line in report.stale_lines():
            typer.echo(line)
        typer.echo("")
        typer.echo("Bank the reduction: remove the stale line(s), or run `t3 tool test-path-mirror --update-baseline`.")


def _update_baseline(pyproject: Path, root: Path, *, allow_regression: bool) -> None:
    ledger = Ledger.path_for(pyproject)
    if ledger is None:
        typer.echo(
            "No `[tool.teatree.test_path_mirror] baseline_file` configured — nothing to update.",
            err=True,
        )
        raise typer.Exit(code=1)

    committed = load_config(pyproject).grandfathered
    live = frozenset(v.path for v in find_violations(root))
    added = sorted(live - committed)
    if added and not allow_regression:
        typer.echo(
            f"Refusing to add {len(added)} new grandfathered entry(ies): the ratchet only moves down. "
            "Relocate the new mis-pathed test file(s), or pass --allow-regression to record an "
            "intentional, reviewed rise:\n  " + "\n  ".join(added),
            err=True,
        )
        raise typer.Exit(code=1)

    Ledger.write(ledger, live)
    banked = len(committed - live)
    direction = f"loosened (+{len(added)} added, regression allowed)" if added else f"ratcheted (-{banked} banked)"
    typer.echo(f"Ledger {direction}: {len(live)} grandfathered path(s).")


@tool_app.command("test-path-mirror")
def run_test_path_mirror(
    root: Path = typer.Option(None, "--root", help="Repo root to analyse (default: cwd)"),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    update_baseline: bool = typer.Option(
        False, "--update-baseline", help="Rewrite the committed grandfathered ledger to the exact live violation set."
    ),
    allow_regression: bool = typer.Option(
        False,
        "--allow-regression",
        help="With --update-baseline, permit ADDING a new grandfathered entry "
        "(an intentional, reviewed rise). Refused by default so the ratchet cannot silently loosen.",
    ),
) -> None:
    """Forward-guard: test files mirror their ``src/teatree/<pkg>/...`` module path.

    Per-path ledger (RED on a live violation missing from the ledger, RED on a
    stale ledger entry that no longer violates), so the relocation sweep can only
    shrink the floor and disjoint PRs never collide. A CI / report check, never a
    PreToolUse gate — it can never lock the agent's tools.
    """
    resolved = _resolve_root(root)
    pyproject = resolved / "pyproject.toml"

    if update_baseline:
        _update_baseline(pyproject, resolved, allow_regression=allow_regression)
        return

    config = load_config(pyproject)
    report = build_report(root=resolved, config=config)

    if output_json:
        typer.echo(_report_json(report))
    else:
        _print_report(report)

    if report.failed:
        raise typer.Exit(code=1)
