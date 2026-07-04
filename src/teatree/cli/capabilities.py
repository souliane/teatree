"""``t3 capabilities`` — the machine-readable capability registry (PR-30).

A front-end discovers teatree's machine interface here instead of scraping
``--help``: which commands emit JSON and their exit-code contract. Pure data
(``teatree.core.capabilities``) — no Django bootstrap needed. Follows the same
stdout/stderr split as the ``emit`` seam: ``--json`` emits the registry on
stdout; the human listing goes to stderr so stdout stays a clean JSON channel.
"""

import json as _json

import typer

from teatree.core.capabilities import CapabilitiesReport, capabilities_report


def capabilities(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the capability registry as JSON on stdout."),
) -> None:
    """List each command's --json support and exit-code contract (front-end discovery)."""
    report = capabilities_report()
    if json_output:
        typer.echo(_json.dumps(report))
        return
    _render_human(report)


def _render_human(report: CapabilitiesReport) -> None:
    typer.echo(f"t3 machine-interface capabilities (v{report['version']}):", err=True)
    for entry in report["commands"]:
        flag = "--json" if entry["json"] else "no-json"
        codes = ",".join(entry["exit_codes"])
        line = f"  {entry['command']:<28} {flag:<8} exit[{codes}]"
        note = entry.get("note")
        typer.echo(f"{line}  ({note})" if note else line, err=True)


__all__ = ["capabilities"]
