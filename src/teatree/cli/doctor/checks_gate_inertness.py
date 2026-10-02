"""`t3 doctor` shipped-inert-feature advisory — a gate merged and never fired (#4189).

The feature twin of ``_check_shipped_seed_inertness``, and it echoes the same half: only
FAULTS, because a gate the owner deliberately staged is doing exactly what staging it asked
for, and a check that reports those every hour is one people learn to ignore. The full
report, notes included, is ``t3 <overlay> config_setting inert``.
"""

import typer


def _check_gates_shipped_inert() -> None:
    """Name every gate that is off everywhere and has produced no evidence of ever running.

    Surfacing-only — it never gates the exit code, like the sibling ORM-reading advisories,
    because what to do about an inert gate stays an owner decision. Crash-proof: any error
    degrades to one WARN line so one broken probe cannot hide every other finding.
    """
    from teatree.core.factory.feature_inertness import feature_inertness  # noqa: PLC0415 — deferred: ORM-reading import

    try:
        faults = [finding for finding in feature_inertness() if finding.is_fault]
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Feature-inertness check crashed: {exc.__class__.__name__}: {exc}")
        return
    if not faults:
        return
    for finding in faults:
        typer.echo(f"WARN  Gate shipped inert: {finding.label}")
    typer.echo("WARN  Run `t3 <overlay> config_setting inert` for the full report, including the staged ones.")
