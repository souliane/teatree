"""`t3 doctor` drifted-config-row advisory — a stored row shadowing a shipped default (#4074).

``ConfigSetting`` holds DELTAS from ``defaults.toml``; the file is the floor. Nothing reports
when the floor moves UNDER a row. ``config_setting get`` answers ``[source: db, global]`` —
accurate, and silent about the default having changed since the row was written.

The instance that motivated this: ``issue_implementer_label`` sat in the DB as the retired
``t3-batch`` while the shipped default had become ``t3-auto``. Reading the DB value made the
config look correct, then like a misconfiguration, then like the owner misremembering — three
wrong conclusions and about an hour, settled only by reading the config history and
BLUEPRINT. The value was readable the whole time; its DIVERGENCE from the shipped default was
not. That is the #4041 shape at the config layer.

**Advisory, never a failure.** Most divergent rows are deliberate operator intent and must
stay exactly as they are — this check has no idea which, and guessing would make it useless.
It changes no exit code and gates nothing. What it removes is the indistinguishability: a
drifted row and an intended one look identical today, and after this they are at least both
NAMED, with the shipped value beside the stored one so the reader can judge in one line.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import typer
from django.utils import timezone

from teatree.config.declared_defaults import declared_defaults
from teatree.config.write_bounds import bound_refusal
from teatree.core.config_display import masked_display
from teatree.core.time_format import format_age


@dataclass(frozen=True, slots=True)
class StoredRow:
    key: str
    scope: str
    value: object
    written_by: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DriftedSetting:
    """One stored row that does not match the shipped default for its key."""

    key: str
    scope: str  # "" is global
    stored: object
    shipped: object
    writer: str = ""
    age: timedelta = timedelta(0)

    @property
    def where(self) -> str:
        return self.scope or "global"


def drifted_settings(
    *, stored: Iterable[StoredRow], shipped: Mapping[str, object], now: datetime | None = None
) -> tuple[DriftedSetting, ...]:
    """The rows in *stored* whose value differs from *shipped*'s default for the same key.

    Pure, so the judgement is testable without a database and the doctor wrapper is left with
    only the read. *stored* carries one ``StoredRow`` per ``ConfigSetting`` row,
    NOT a resolved effective value: the question is "does this ROW disagree with the file",
    and resolving first would hide a shadowed row behind a higher tier that happens to agree.

    A key the shipped table carries no entry for is SKIPPED. There is no shipped value to
    diverge from, so any drift claim would be invented — and the Personal/Secret keys absent
    from the file by construction would otherwise every one of them read as drift, burying the
    real findings. Such a key still has a code default; comparing against it is a different
    check with a different failure mode, deliberately not folded in here.

    Each scope is judged on its own row: an overlay override and the global row are separate
    overrides of the same floor, and only one of them may have gone stale.
    """
    clock = now or timezone.now()
    return tuple(
        DriftedSetting(
            row.key,
            row.scope,
            row.value,
            shipped[row.key],
            row.written_by,
            clock - row.updated_at if row.updated_at else timedelta(0),
        )
        for row in stored
        if row.key in shipped and row.value != shipped[row.key]
    )


def _stored_rows() -> list[StoredRow]:
    """Every stored override, including its provenance and age."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

    return [
        StoredRow(row.key, row.scope, row.value, row.written_by, row.updated_at)
        for row in ConfigSetting.objects.all().order_by("key", "scope")
    ]


def _finding_line(drift: DriftedSetting) -> str:
    """One advisory line — key, scope, stored value, shipped value, both mask-safe.

    A drifted SECRET still has to surface: a stale secret row is exactly as misleading as any
    other, and omitting it would make the check quietly incomplete on the keys that matter
    most. Both values go through the shared masking taxonomy
    (:func:`~teatree.core.config_display.masked_display`), so the row is named without either
    value reaching the output.
    """
    stored = masked_display(drift.key, drift.stored)
    shipped = masked_display(drift.key, drift.shipped)
    return (
        f"INFO  {drift.key} [{drift.where}] is {stored}; shipped default is {shipped}; "
        f"written by {drift.writer or 'unknown'} {format_age(drift.age)} ago"
    )


def _check_config_rows_shadowing_shipped_defaults() -> None:
    """List every stored config row that differs from its shipped default (#4074).

    Surfacing-only — it never gates the exit code, like the sibling ORM-reading advisories,
    because a divergent row is usually intent. Crash-proof: any error degrades to one WARN
    line so a doctor run always completes and one broken probe cannot hide every other
    finding.
    """
    try:
        rows = _stored_rows()
        drifted = drifted_settings(stored=rows, shipped=declared_defaults())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Config-drift check crashed: {exc.__class__.__name__}: {exc}")
        return
    if drifted:
        typer.echo(
            f"INFO  {len(drifted)} stored config row(s) differ from the shipped default. "
            "Most are deliberate; a stale one is the same shape as #4074. "
            "Clear one with `t3 <overlay> config_setting clear <key>`."
        )
    for drift in drifted:
        typer.echo(_finding_line(drift))
    for row in rows:
        if reason := bound_refusal(row.key, row.value):
            typer.echo(f"WARN  {row.key} [{row.scope or 'global'}] is below its write bound: {reason}")
