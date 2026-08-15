"""``t3 tool dependency-audit`` — reachability assessment for audit findings.

Registers onto the shared ``tool_app`` (mirroring ``test_shape_tools``). The
analysis lives in :mod:`teatree.quality.dependency_audit`; this module resolves
the repo root, reads a ``pip-audit --format json`` report, and prints each
advisory annotated with whether the affected component is reachable from
``src/``.

A report, not a gate: the blocking verdict stays ``pip-audit``'s own exit code
in the ``uv-audit`` job. Exit 1 is reserved for an unreadable report, which is
a failure of the assessment itself rather than a finding.
"""

import json
import sys
from pathlib import Path

import typer

from teatree.quality.dependency_audit import ReportError, annotate, build_import_index, format_report, parse_report


def _read(report: str) -> str:
    return sys.stdin.read() if report == "-" else Path(report).read_text(encoding="utf-8")


def run_dependency_audit(
    report: str = typer.Option(..., "--report", help="pip-audit --format json report ('-' for stdin)."),
    root: Path = typer.Option(None, "--root", help="Repo root whose src/ is indexed (default: cwd)"),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Annotate each dependency-audit finding with its reachability from ``src/``."""
    try:
        advisories = parse_report(_read(report))
    except (OSError, ReportError) as exc:
        typer.echo(f"dependency-audit: could not read the audit report: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    index = build_import_index((root or Path.cwd()).resolve() / "src")
    annotated = annotate(advisories, index=index)

    if output_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "package": entry.advisory.package,
                        "version": entry.advisory.version,
                        "id": entry.advisory.vuln_id,
                        "aliases": list(entry.advisory.aliases),
                        "import_names": list(entry.import_names),
                        "basis": entry.basis.value,
                        "package_reach": entry.package_reach.value,
                        "components": [{"module": c.module, "reach": c.reach.value} for c in entry.components],
                    }
                    for entry in annotated
                ],
                indent=2,
            )
        )
        return
    typer.echo(format_report(annotated))


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("dependency-audit")(run_dependency_audit)
