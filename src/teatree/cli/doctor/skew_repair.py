"""Report — or, under ``--repair``, clear — declared-versus-installed skew (#4049).

Two properties this module exists to hold.

**A bare ``t3 doctor check`` never reinstalls.** The check runs at ``SessionStart``,
under the very console script a ``uv tool install --reinstall`` replaces, so repairing
without being asked swaps the running program out from under the session. ``--repair``
is the flag every other mutating doctor check already threads; skew now threads it too,
and the read-only path prints the exact command instead of running it.

**The already-attempted guard is durable.** Its first spelling wrote to the process's
own ``os.environ`` and read it back inside the same one-call-per-process function, so it
could never fire — nothing under ``teatree.cli.doctor`` re-execs, unlike
:mod:`teatree.cli.dep_drift_repair`, whose identical env guard survives precisely
because it ``execv``s. Combined with the unconditional repair above, an unrepairable
skew reinstalled the env on every single run.

The substrate is a file in this venue's own data dir, not a control-DB row: the control
DB is a docker named volume that host ``t3`` commands cannot open, and the host tool env
is exactly the side whose skew this records. ``data_dir_root()`` resolves per venue, so
host and container keep separate receipts — which is what the finding needs, since the
two installs drift independently. The receipt is keyed on the skew it was written for,
so fresh drift is still repaired once while the same unrepaired drift reports itself.
"""

import hashlib
import json
import time
from collections.abc import Iterable
from pathlib import Path

import typer

from teatree.utils.dep_skew import VersionSkew

RECEIPT_FILENAME = "mcp-skew-repair.json"


def receipt_path() -> Path:
    """The receipt file for THIS venue's install.

    Resolved at call time, never at import: ``data_dir_root`` reads ``T3_DATA_DIR``, and
    a caller that points it elsewhere must be honoured for the read that follows.
    """
    from teatree.paths import data_dir_root  # noqa: PLC0415 — deferred: env-sensitive path resolution at call time

    return data_dir_root() / RECEIPT_FILENAME


def skew_fingerprint(skews: Iterable[VersionSkew]) -> str:
    """A stable key for one skew SET, so a different drift is a different receipt."""
    digest = hashlib.sha256("\n".join(sorted(skew.summary for skew in skews)).encode("utf-8"))
    return digest.hexdigest()


def repair_already_attempted(fingerprint: str) -> bool:
    """Has a repair for this exact skew already run and left it in place?

    An unreadable or malformed receipt answers ``False``: the cost of being wrong that
    way is one extra reinstall the operator explicitly asked for with ``--repair``,
    while the other way strands them with a skew nothing will clear.
    """
    try:
        record = json.loads(receipt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("fingerprint") == fingerprint


def record_repair_attempt(fingerprint: str) -> None:
    """Claim the attempt BEFORE running it, so a repair killed mid-flight still counts."""
    path = receipt_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fingerprint": fingerprint, "attempted_at": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:
        typer.echo(f"      Could not record the repair attempt at {path}: {exc}")


def clear_repair_receipt() -> None:
    """Drop the receipt once the skew is gone, so the same drift is repairable again."""
    receipt_path().unlink(missing_ok=True)


def _remedy(skews: list[VersionSkew]) -> str | None:
    """The command that clears *skews* in the running env, or ``None`` when there is none."""
    from teatree.cli.dep_drift_repair import (  # noqa: PLC0415 — deferred: repair path only
        RepairPlan,
        resolve_repair_plan,
    )

    plan = resolve_repair_plan([skew.name for skew in skews])
    return plan.label if isinstance(plan, RepairPlan) else None


def report_version_skew(skews: list[VersionSkew]) -> None:
    """Print the exact remedy for *skews* and run nothing — the read-only default."""
    remedy = _remedy(skews)
    if remedy is None:
        typer.echo("      No self-repair applies to this install — reinstall teatree into the running env.")
        return
    typer.echo(f"      Fix it by running: `{remedy}`")
    typer.echo("      Or re-run as `t3 doctor check --repair` to have the doctor run that for you.")


def repair_version_skew(source: Path, skews: list[VersionSkew]) -> bool:
    """Reinstall the running env to clear *skews*; return whether they are gone.

    A stale env is a MECHANICAL cause with a deterministic fix, so under ``--repair`` it
    is repaired rather than escalated — the operator is interrupted only for the causes
    that need judgement.
    """
    from teatree.utils.dep_skew import find_version_skew  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: repair path only

    fingerprint = skew_fingerprint(skews)
    if repair_already_attempted(fingerprint):
        typer.echo(f"      A repair for this exact skew already ran and it persists (see {receipt_path()}).")
        report_version_skew(skews)
        return False

    from teatree.cli.dep_drift_repair import (  # noqa: PLC0415 — deferred: repair path only
        RepairPlan,
        resolve_repair_plan,
    )

    plan = resolve_repair_plan([skew.name for skew in skews])
    if not isinstance(plan, RepairPlan):
        typer.echo(f"      Cannot self-repair: {plan}")
        return False

    typer.echo(f"      Self-repairing the stale env — running `{plan.label}` …")
    record_repair_attempt(fingerprint)
    result = run_allowed_to_fail(plan.cmd, expected_codes=None)
    if result.returncode != 0:
        typer.echo(f"      Repair FAILED: {result.stderr.strip()[:400]}")
        typer.echo(f"      Manual fix: `{plan.label}`.")
        return False
    remaining = find_version_skew(source / "pyproject.toml")
    if remaining:
        typer.echo(f"      Repair ran but skew persists: {'; '.join(skew.summary for skew in remaining)}")
        return False
    clear_repair_receipt()
    typer.echo("      Repaired — the env now satisfies every declared runtime dependency.")
    return True
