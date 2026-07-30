"""The SEED-table half of the config export/import — ``[loops]`` / ``[modes]`` / ``[schedules]``.

The shipped defaults an operator tunes are not only ``ConfigSetting`` keys: the loops,
modes and schedules are too. They ride the SAME override rule as a setting — only a field
tuned away from its ``defaults.toml`` seed is exported — but they live in their own models,
carry no operator secrets, and take the same path in a shared and a private export. That
is a different concern from the ``ConfigSetting`` store's secret-guarded rows, so it lives
in its own module; :mod:`teatree.core.config_migration` composes the two.

The dependency runs one way: this module knows nothing of the export's row dataclasses. It
answers in :class:`SeedFieldDisposition`, and the caller maps each one onto its own
written / skipped / rejected shape.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.seed_defaults import SEED_ROW_FIELDS, SEED_TABLES, classify_seed_field, seed_divergences
from teatree.core.models import Loop, Mode, ModeSchedule
from teatree.core.models.config_setting import ConfigValue

#: The model each seed table's rows live in — the DB half of ``SEED_ROW_FIELDS``.
_SEED_MODELS = {"loops": Loop, "modes": Mode, "schedules": ModeSchedule}

#: Renders one ``{key: value}`` mapping as a key-sorted TOML table.
type SortedTableFactory = Callable[[dict[str, ConfigValue]], tomlkit_items.Table]


@dataclass(frozen=True, slots=True)
class SeedFieldDisposition:
    """One seed field an import classified, and what the classifier decided about it."""

    table: str
    name: str
    field: str
    value: ConfigValue
    kind: str  # "write" / "skip" / "reject"
    reason: str

    @property
    def scope(self) -> str:
        """The row's scope label in an import report — ``<family>.<entry name>``."""
        return f"{self.table}.{self.name}"


def live_seed_rows(table: str) -> dict[str, dict[str, ConfigValue]]:
    """Every row of *table*'s model as ``{name: {seed field: value}}``."""
    fields = SEED_ROW_FIELDS[table]
    return {
        row.name: {seed_field: getattr(row, attr) for seed_field, (attr, _type) in fields.items()}
        for row in _SEED_MODELS[table].objects.all()
    }


def emit_seed_tables(document: tomlkit.TOMLDocument, sorted_table: SortedTableFactory) -> None:
    """Attach a ``[<family>.<name>]`` sub-table per seed row that diverges from its default.

    *sorted_table* is the caller's key-sorted table renderer, so the seed tables and the
    settings tables come out of one formatting path.
    """
    for table in SEED_TABLES:
        diverged = seed_divergences(table, live_seed_rows(table))
        if not diverged:
            continue
        family = tomlkit.table(is_super_table=True)
        for name in sorted(diverged):
            family[name] = sorted_table(diverged[name])
        document[table] = family


def classify_seed_rows(doc: dict[str, Any]) -> list[SeedFieldDisposition]:
    """Classify every seed field the document carries against what ``defaults.toml`` ships."""
    dispositions = []
    for table in SEED_TABLES:
        for name, entry in doc.get(table, {}).items():
            if not isinstance(entry, dict):
                continue
            for field, value in entry.items():
                kind, reason = classify_seed_field(table, name, field, value)
                dispositions.append(SeedFieldDisposition(table, name, field, value, kind, reason))
    return dispositions


def unseeded_entries(writes: list[SeedFieldDisposition]) -> set[tuple[str, str]]:
    """The ``(table, name)`` pairs *writes* target that have no DB row yet.

    An entry the shipped file carries can still have no row on a box that never ran the
    install seed, so the caller refuses the whole import rather than letting a write raise
    mid-run.
    """
    return {(w.table, w.name) for w in writes if not _SEED_MODELS[w.table].objects.filter(name=w.name).exists()}


def write_seed_field(table: str, name: str, field: str, value: ConfigValue) -> None:
    """Set one seed field onto the row it names — never creating one.

    An import RESTORES an operator's tuning onto objects the install seed already made; it
    never conjures a loop teatree does not ship (the classifier refuses an entry the file
    does not carry, and the caller refuses one whose row is not seeded yet).
    """
    attr, _type = SEED_ROW_FIELDS[table][field]
    row = _SEED_MODELS[table].objects.get(name=name)
    setattr(row, attr, value)
    row.save(update_fields=[attr, "updated_at"])
