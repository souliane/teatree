"""Whether two instances may be compared at all — the signal table and its verdict.

A settings diff taken across two boxes that disagree about what settings EXIST is worse than
no diff at all: every key one box's code lacks reads as a difference in configuration. So the
comparison is gated on the two signals that answer "do these boxes agree about the shape of
the schema", and everything else is reported beside the verdict rather than folded into it.

Three severities, and the difference between them is what a disagreement MEANS:

*   ``BLOCKING`` — the diff below cannot be trusted; fix the skew before reading it.
*   ``WARN`` — the diff is readable, but this difference explains rows in it.
*   ``INFO`` — context. Never an input to the verdict, however loudly it differs.

**A loaded record cannot make the live boxes incomparable.** The verdict answers "may these
boxes be diffed", which is a question about the boxes that are RUNNING; a snapshot file
disagreeing with them says only that time has passed since it was captured, and an older
capture legitimately declares fewer settings than the code does today. So agreement is read
across the LIVE columns alone and a file's differing reading is reported as :attr:`CompatRow.dated`
at ``INFO``. It is never lost — the row still shows it, marked, so the reader sees WHICH column
is the record and how far it has drifted. The value diff stays honest either way: a key one
side's code never declared compares EQUAL there rather than reading as configuration
(:mod:`teatree.dash.settings_compare`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from teatree.core.settings_snapshot import canonical_json
from teatree.dash.settings_peers import PeerSnapshot, SnapshotOrigin


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class CompatSignal:
    """One fingerprint field, and what a disagreement about it means for the diff."""

    field: str
    label: str
    severity: Severity
    note: str


#: The whole signal table, in report order. Adding a fingerprint field means adding a row here
#: with its severity decided ONCE, rather than a second opinion at each reading surface.
COMPAT_SIGNALS: tuple[CompatSignal, ...] = (
    CompatSignal(
        "settings_schema_sha256",
        "settings schema",
        Severity.BLOCKING,
        "the boxes declare different settings, so a value diff cannot be read as configuration drift",
    ),
    CompatSignal(
        "settings_key_count",
        "settings key count",
        Severity.BLOCKING,
        "one box knows more settings than the other — the extra keys are code, not configuration",
    ),
    CompatSignal(
        "physical_schema_sha256",
        "database schema",
        Severity.WARN,
        "the control databases differ in shape; seed rows may not mean the same thing on both",
    ),
    CompatSignal(
        "migration_leaves",
        "migration leaves",
        Severity.WARN,
        "the boxes sit at different migration heads",
    ),
    CompatSignal(
        "pending_migrations",
        "pending migrations",
        Severity.WARN,
        "a box mid-deploy has provisional readings",
    ),
    CompatSignal(
        "defaults_toml_sha256",
        "shipped defaults.toml",
        Severity.WARN,
        "the shipped defaults differ, so 'same as default' is not the same claim on both",
    ),
    CompatSignal(
        "seed_fields_sha256",
        "seed field shape",
        Severity.WARN,
        "the loops / presets / schedules carry different fields",
    ),
    CompatSignal(
        "django_version",
        "Django version",
        Severity.INFO,
        "context: a schema digest moves with the Django version that recorded the DDL",
    ),
    CompatSignal("table_count", "table count", Severity.INFO, "context"),
    CompatSignal(
        "applied_migration_count",
        "applied migrations",
        Severity.INFO,
        "context — never an input to the verdict: a squash leaves rows for migrations that no longer exist",
    ),
)


@dataclass(frozen=True, slots=True)
class CompatReading:
    """One instance's answer for one signal, and whether it matches what the live boxes say."""

    text: str
    origin: SnapshotOrigin
    matches: bool


@dataclass(frozen=True, slots=True)
class CompatRow:
    """One signal read across every instance."""

    signal: CompatSignal
    readings: tuple[str, ...]
    #: One origin per reading; an empty tuple reads every column as live.
    origins: tuple[SnapshotOrigin, ...] = ()

    @property
    def cells(self) -> tuple[CompatReading, ...]:
        live = self._live
        return tuple(
            CompatReading(reading, origin, not reading or reading in live)
            for reading, origin in zip(self.readings, self._origins, strict=True)
        )

    @property
    def agrees(self) -> bool:
        """A field NO instance reported is not a disagreement — it is a source that failed."""
        return len(self._live) <= 1

    @property
    def dated(self) -> bool:
        """A loaded record read against different code — context, never an input to the verdict."""
        live = self._live
        return any(cell.text and cell.text not in live for cell in self.cells if cell.origin is SnapshotOrigin.FILE)

    @property
    def severity(self) -> Severity:
        return Severity.INFO if self.agrees and self.dated else self.signal.severity

    @property
    def _origins(self) -> tuple[SnapshotOrigin, ...]:
        return self.origins or (SnapshotOrigin.LIVE,) * len(self.readings)

    @property
    def _live(self) -> frozenset[str]:
        return frozenset(
            reading
            for reading, origin in zip(self.readings, self._origins, strict=True)
            if reading and origin is SnapshotOrigin.LIVE
        )


@dataclass(frozen=True, slots=True)
class CompatReport:
    """The signal table read across the instances, and the one verdict drawn from it."""

    labels: tuple[str, ...]
    rows: tuple[CompatRow, ...]

    @property
    def blocking(self) -> tuple[CompatRow, ...]:
        return tuple(row for row in self.rows if row.severity is Severity.BLOCKING and not row.agrees)

    @property
    def warnings(self) -> tuple[CompatRow, ...]:
        return tuple(row for row in self.rows if row.severity is Severity.WARN and not row.agrees)

    @property
    def dated(self) -> tuple[CompatRow, ...]:
        """The signals a loaded record reads differently — the record's age, never drift."""
        return tuple(row for row in self.rows if row.dated)

    @property
    def comparable(self) -> bool:
        """Whether the value diff below can be read as configuration drift at all."""
        return not self.blocking

    @property
    def verdict(self) -> str:
        if not self.comparable:
            return "not comparable — " + "; ".join(row.signal.note for row in self.blocking)
        if self.warnings:
            return "comparable, with differences that explain rows below"
        if self.dated:
            return "comparable — a loaded record was captured against different code, which is its age, not drift"
        return "comparable"


def build_compat_report(instances: Sequence[PeerSnapshot]) -> CompatReport:
    """Read every signal across *instances* — an unreachable one reports nothing, never a value."""
    origins = tuple(instance.origin for instance in instances)
    return CompatReport(
        labels=tuple(instance.label for instance in instances),
        rows=tuple(
            CompatRow(signal, tuple(_reading(instance.fingerprint, signal.field) for instance in instances), origins)
            for signal in COMPAT_SIGNALS
        ),
    )


def _reading(fingerprint: Mapping[str, Any], field: str) -> str:
    """*field* as stable text, or ``""`` when this instance did not report it.

    The empty string is "nothing to say", never a value: an instance whose optional source
    failed is left out of the agreement test rather than counted as a differing reading.
    """
    if field not in fingerprint:
        return ""
    value = fingerprint[field]
    return value if isinstance(value, str) else canonical_json(value)


__all__ = [
    "COMPAT_SIGNALS",
    "CompatReading",
    "CompatReport",
    "CompatRow",
    "CompatSignal",
    "Severity",
    "build_compat_report",
]
