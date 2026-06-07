"""``t3 tool test-path-mirror`` — the test-files-mirror-src forward-guard.

Registers onto the shared ``tool_app`` (side-effect import from
``cli/__init__``, mirroring ``test_shape_tools`` / ``triage_tools``). The
analysis lives in :mod:`teatree.quality.test_path_mirror`; this module is the
thin CLI surface: it resolves the repo root, loads
``[tool.teatree.test_path_mirror]``, builds the report, prints it, and chooses
the exit code from the ratchet.

Exit codes:

* ``0`` — live violation count at or below the committed baseline.
* ``1`` — live violation count ABOVE the baseline (a new mis-pathed file slipped in).

``--update-baseline`` rewrites the committed floor in ``pyproject.toml`` (via
``tomlkit`` so formatting/comments survive) to the current count. The ratchet
only moves down: an update that would write a HIGHER count than the committed
baseline is REFUSED unless ``--allow-regression`` is also passed. Without that
guard the gate is vacuous — a regression could be "fixed" by silently
re-baselining to the regressed value. The escape exists for an intentional,
reviewed rise, but it must be an explicit, visible choice.
"""

import json
from pathlib import Path

import typer

from teatree.cli.tools import tool_app
from teatree.quality.test_path_mirror import MirrorReport, build_report, find_violations, load_config, loosens_baseline


def _resolve_root(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _report_json(report: MirrorReport) -> str:
    return json.dumps(
        {
            "baseline": report.baseline,
            "live_count": report.live_count,
            "exceeds_baseline": report.exceeds_baseline,
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
    if not report.exceeds_baseline:
        typer.echo(
            f"test-path-mirror: {report.live_count} violation(s) at/below baseline {report.baseline} "
            "(test files mirror src — ratchet holds)."
        )
        return
    typer.echo(f"test-path-mirror REGRESSION: {report.live_count} violation(s) exceed baseline {report.baseline}.")
    typer.echo("")
    for line in report.summary_lines():
        typer.echo(line)
    typer.echo("")
    typer.echo(
        "A test file must mirror its src/teatree/<pkg>/... module path as tests/teatree_<pkg>/... . "
        "Move the new file, or (for a genuine multi-package contract test) add a "
        "`# test-path: cross-cutting` pragma."
    )


def _update_baseline(pyproject: Path, root: Path, *, allow_regression: bool) -> None:
    import tomlkit  # noqa: PLC0415

    measured = len(find_violations(root))
    committed = load_config(pyproject).baseline
    loosening = loosens_baseline(measured=measured, baseline=committed)
    if loosening and not allow_regression:
        typer.echo(
            f"Refusing to loosen the baseline: measured count {measured} is above the committed "
            f"baseline {committed}. The ratchet only moves down. Relocate the new mis-pathed "
            "test file(s), or pass --allow-regression to record an intentional, reviewed rise.",
            err=True,
        )
        raise typer.Exit(code=1)

    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    table = doc.setdefault("tool", {}).setdefault("teatree", {}).setdefault("test_path_mirror", tomlkit.table())
    table["baseline"] = measured
    pyproject.write_text(tomlkit.dumps(doc), encoding="utf-8")
    direction = "loosened (regression allowed)" if loosening else "ratcheted"
    typer.echo(f"Baseline {direction}: baseline={measured}.")


@tool_app.command("test-path-mirror")
def run_test_path_mirror(
    root: Path = typer.Option(None, "--root", help="Repo root to analyse (default: cwd)"),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    update_baseline: bool = typer.Option(
        False, "--update-baseline", help="Rewrite the committed violation-count baseline to the current measurement."
    ),
    allow_regression: bool = typer.Option(
        False,
        "--allow-regression",
        help="With --update-baseline, permit writing a HIGHER count than the committed baseline "
        "(an intentional, reviewed rise). Refused by default so the ratchet cannot silently loosen.",
    ),
) -> None:
    """Forward-guard: test files mirror their ``src/teatree/<pkg>/...`` module path.

    Baseline-ratchet (fails only when the live mis-pathed count exceeds the
    committed baseline), so the relocation sweep can only shrink the floor. A CI /
    report check, never a PreToolUse gate — it can never lock the agent's tools.
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

    if report.exceeds_baseline:
        raise typer.Exit(code=1)
