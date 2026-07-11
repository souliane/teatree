"""``t3 tool test-shape`` — the conservative, report-first test-shape check.

Registers onto the shared ``tool_app`` (side-effect import from
``cli/__init__``, mirroring ``enforcement_tools`` / ``triage_tools``). The
analysis lives in :mod:`teatree.quality.test_shape`; this module is the thin
CLI surface: it resolves the repo root, loads ``[tool.teatree.test_shape]``,
builds the report, prints it, and chooses the exit code from the report's mode.

Exit codes:

* ``0`` — no findings, OR findings under the default ``warn`` mode (advisory).
* ``1`` — findings under the opt-in ``block`` mode only.

``--update-baseline`` rewrites the committed ratio snapshot in
``pyproject.toml`` (via ``tomlkit`` so formatting/comments survive) to the
current measurement. The ratchet only moves in the improving direction: an
update that would write a WORSE test:source ratio than the committed baseline
is REFUSED unless ``--allow-regression`` is also passed. Without that guard the
check is vacuous — a regression could be "fixed" by silently re-baselining to
the regressed value. The escape exists for an intentional, reviewed drop, but
it must be an explicit, visible choice — never the default behaviour.
"""

import json
from pathlib import Path

import typer

from teatree.quality.test_shape import (
    Mode,
    TestShapeReport,
    build_report,
    collect_source_files,
    collect_test_files,
    load_config,
    loosens_baseline,
    measure_ratio,
)


def _resolve_root(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _report_json(report: TestShapeReport) -> str:
    return json.dumps(
        {
            "mode": report.mode.value,
            "has_findings": report.has_findings,
            "should_block": report.should_block,
            "duplicate_clusters": [{"path": c.path, "functions": list(c.functions)} for c in report.duplicate_clusters],
            "ratio_regression": (
                {
                    "measured_ratio": report.ratio_regression.measured.ratio,
                    "measured_test_lines": report.ratio_regression.measured.test_lines,
                    "measured_source_lines": report.ratio_regression.measured.source_lines,
                    "baseline_ratio": report.ratio_regression.baseline.ratio,
                    "tolerance": report.ratio_regression.baseline.tolerance,
                }
                if report.ratio_regression is not None
                else None
            ),
        },
        indent=2,
    )


def _print_report(report: TestShapeReport) -> None:
    if not report.has_findings:
        typer.echo("test-shape: no findings (test:source ratio at/above baseline, no unparametrized duplicates).")
        return
    severity = "BLOCK" if report.mode is Mode.BLOCK else "WARN (advisory — not blocking)"
    typer.echo(f"test-shape findings [{severity}]:")
    typer.echo("")
    for line in report.summary_lines():
        typer.echo(line)
    typer.echo("")
    if report.mode is Mode.WARN:
        typer.echo('Advisory only. Set [tool.teatree.test_shape] mode = "block" in pyproject.toml to enforce.')


def _update_baseline(pyproject: Path, root: Path, *, allow_regression: bool) -> None:
    import tomlkit  # noqa: PLC0415

    measured = measure_ratio(
        test_files=collect_test_files(root),
        source_files=collect_source_files(root),
    )
    committed = load_config(pyproject).baseline
    loosening = committed is not None and loosens_baseline(measured, committed)
    if committed is not None and loosening and not allow_regression:
        typer.echo(
            f"Refusing to loosen the baseline: measured ratio {measured.ratio:.3f} "
            f"(test {measured.test_lines} / source {measured.source_lines}) is below the "
            f"committed baseline {committed.ratio:.3f} "
            f"(test {committed.test_lines} / source {committed.source_lines}). "
            "The ratchet only moves in the improving direction. Add tests to recover the "
            "ratio, or pass --allow-regression to record an intentional, reviewed drop.",
            err=True,
        )
        raise typer.Exit(code=1)

    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    table = doc.setdefault("tool", {}).setdefault("teatree", {}).setdefault("test_shape", tomlkit.table())
    table["test_lines"] = measured.test_lines
    table["source_lines"] = measured.source_lines
    pyproject.write_text(tomlkit.dumps(doc), encoding="utf-8")
    direction = "loosened (regression allowed)" if loosening else "ratcheted"
    typer.echo(
        f"Baseline {direction}: test_lines={measured.test_lines}, "
        f"source_lines={measured.source_lines} (ratio {measured.ratio:.3f})."
    )


def run_test_shape(
    root: Path = typer.Option(None, "--root", help="Repo root to analyse (default: cwd)"),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    update_baseline: bool = typer.Option(
        False, "--update-baseline", help="Rewrite the committed test:source baseline to the current measurement."
    ),
    allow_regression: bool = typer.Option(
        False,
        "--allow-regression",
        help="With --update-baseline, permit writing a WORSE ratio than the committed baseline "
        "(an intentional, reviewed drop). Refused by default so the ratchet cannot silently loosen.",
    ),
) -> None:
    """Conservative test-shape check: near-duplicate tests + test:source ratio regression.

    Baseline-ratchet (fails only on regression past the committed baseline),
    report-first (advisory ``warn`` by default; ``block`` is opt-in). A CI /
    report check, never a PreToolUse gate — it can never lock the agent's tools.
    """
    resolved = _resolve_root(root)
    pyproject = resolved / "pyproject.toml"

    if update_baseline:
        _update_baseline(pyproject, resolved, allow_regression=allow_regression)
        return

    config = load_config(pyproject)
    report = build_report(
        test_files=collect_test_files(resolved),
        source_files=collect_source_files(resolved),
        config=config,
        root=resolved,
    )

    if output_json:
        typer.echo(_report_json(report))
    else:
        _print_report(report)

    if report.should_block:
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("test-shape")(run_test_shape)
