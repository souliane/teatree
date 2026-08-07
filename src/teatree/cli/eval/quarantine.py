"""``t3 eval quarantine`` — inspect, validate, and audit the known-red registry (#4173).

Three thin shells over :mod:`teatree.eval.quarantine`; the logic lives there.

``list``
    The entries, with the tracking issue and the expiry the reader needs to act.

``check``
    The static validator: an EXPIRED entry (it no longer suppresses — fix the scenario or
    re-date it), an entry naming a scenario the catalog does not define, or a malformed
    file. Exits non-zero on any of them.

``audit <merged-summary.json>``
    The run-aware half, and the loud channel the heal lane runs beside ``green-proof``. It
    reads a merged §2.4 payload and reports every quarantined scenario's outcome by name:
    STILL RED (expected, tracked — reported, never an extra failure on top of the run's
    own), NOT RUN (the run says nothing about it), and ESCAPED — a quarantined scenario
    that PASSED, which makes the entry a lie and reds the audit until it is deleted.
"""

import json
from pathlib import Path
from typing import Any

import typer

from teatree.eval.discovery import discover_specs
from teatree.eval.quarantine import Quarantine, QuarantineError, load_quarantine, utc_today
from teatree.utils.django_bootstrap import ensure_django

quarantine_app = typer.Typer(help="The known-red quarantine: scenarios the bounded PR lane does not select.")

_FILE_OPTION = typer.Option(None, "--file", help="The registry to read (default: evals/quarantine.yaml).")


def _load_or_exit(path: Path | None) -> Quarantine:
    try:
        return load_quarantine(path)
    except QuarantineError as exc:
        typer.echo(f"MALFORMED QUARANTINE: {exc}")
        raise typer.Exit(1) from exc


@quarantine_app.command("list")
def list_entries(file: Path | None = _FILE_OPTION) -> None:
    """Print every quarantined scenario with its tracking issue and expiry."""
    quarantine = _load_or_exit(file)
    if not quarantine.entries:
        typer.echo("no quarantined scenarios — the bounded PR lane selects everything the diff touches")
        return
    today = utc_today()
    for entry in quarantine.entries:
        state = "EXPIRED" if entry.is_expired(today) else "active"
        typer.echo(f"{state}: {entry.render()}")


@quarantine_app.command("check")
def check(
    file: Path | None = _FILE_OPTION,
    scenario: list[str] = typer.Option(
        None,
        "--scenario",
        help="Catalog scenario name to validate entries against (default: the discovered catalog).",
    ),
) -> None:
    """Validate the registry: no expired entry, no entry naming a scenario that does not exist."""
    quarantine = _load_or_exit(file)
    if scenario:
        catalog = set(scenario)
    else:
        ensure_django()
        catalog = {spec.name for spec in discover_specs()}
    findings = [
        f"EXPIRED {entry.render()} — it no longer suppresses; fix the scenario or re-date the entry"
        for entry in quarantine.expired()
    ]
    findings.extend(
        f"UNKNOWN {entry.render()} — no scenario by that name; a rename or a typo suppresses nothing"
        for entry in quarantine.unknown(catalog=catalog)
    )
    for finding in findings:
        typer.echo(finding)
    if findings:
        raise typer.Exit(1)
    typer.echo(f"quarantine OK: {len(quarantine.entries)} tracked known-red scenario(s), none expired or unknown")


@quarantine_app.command("audit")
def audit(
    summary_json: Path = typer.Argument(..., help="A merged eval-heal §2.4 summary JSON to audit against."),
    file: Path | None = _FILE_OPTION,
) -> None:
    """Report each quarantined scenario's outcome in a run; red on one that has escaped."""
    if not summary_json.is_file():
        typer.echo(f"QUARANTINE AUDIT FAILED: no summary JSON at {summary_json}")
        raise typer.Exit(1)
    quarantine = _load_or_exit(file)
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    passing, failing = _outcomes(payload if isinstance(payload, dict) else {})
    typer.echo(f"QUARANTINE AUDIT: {len(quarantine.entries)} tracked known-red scenario(s)")
    for entry in quarantine.still_red(failing=failing):
        typer.echo(f"  STILL RED {entry.render()}")
    for entry in quarantine.absent(ran=passing | failing):
        typer.echo(f"  NOT RUN {entry.render()}")
    escaped = quarantine.escaped(passing=passing)
    for entry in escaped:
        typer.echo(f"  ESCAPED {entry.render()} — it PASSED; delete the entry so the registry stops lying")
    expired = quarantine.expired()
    for entry in expired:
        typer.echo(f"  EXPIRED {entry.render()} — it no longer suppresses; fix the scenario or re-date the entry")
    if escaped or expired:
        raise typer.Exit(1)


def _outcomes(payload: dict[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    """The (passing, failing) scenario names in a merged payload.

    A row's ``triage_class`` is ``None`` on a pass and a class string on any red — the
    same read :func:`teatree.eval.green_proof.evaluate_green_proof` makes, so the audit
    and the proof can never disagree about which scenarios were red.
    """
    rows = payload.get("scenarios")
    rows = rows if isinstance(rows, list) else []
    passing = {str(row.get("name", "")) for row in rows if isinstance(row, dict) and row.get("triage_class") is None}
    failing = {
        str(row.get("name", ""))
        for row in rows
        if isinstance(row, dict) and "triage_class" in row and row["triage_class"] is not None
    }
    return frozenset(passing), frozenset(failing)
