"""The one fold a retired cadence key gets: its hours onto the Loop row that owned the timer.

A cadence stored in a ``ConfigSetting`` row AND on the ``Loop`` row it gated is two clocks
in series, so retiring the key is right — but the operator's stored hours belong ON the row
rather than in the bin, and the fold has to happen BEFORE the key disappears.

Shared by 0092 and 0096. A migration is a frozen record, so this stays behaviour-identical:
a new fold shape gets a new function here rather than editing this one.

An UNSET key still gated its loop: the reader fell back to the key's retired default, so a
box that never stored one ran on that default, not on the row. :func:`fold_cadence_or_default`
carries that default onto the row, so an unset key and a stored one fold alike.
"""

from collections.abc import Callable

from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

SECONDS_PER_HOUR = 3600

type LoopKeyFolds = tuple[tuple[str, str], ...]
type LoopKeyDefaultFolds = tuple[tuple[str, str, int], ...]


class DivergentScopedRowsError(RuntimeError):
    """The stored rows disagree across scopes, so no single box-global row can replace them."""


def fold_cadence(folds: LoopKeyFolds) -> Callable[[StateApps, BaseDatabaseSchemaEditor], None]:
    """A ``RunPython`` forward that moves each key's stored hours onto its loop, then deletes it."""

    def run(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        for loop_name, key in folds:
            _fold_one(apps, schema_editor.connection.alias, loop_name, key, retired_default=None)

    return run


def fold_cadence_or_default(folds: LoopKeyDefaultFolds) -> Callable[[StateApps, BaseDatabaseSchemaEditor], None]:
    """:func:`fold_cadence`, carrying each key's retired default onto the row when none is stored."""

    def run(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        for loop_name, key, retired_default in folds:
            _fold_one(apps, schema_editor.connection.alias, loop_name, key, retired_default=retired_default)

    return run


def _fold_one(apps: StateApps, db: str, loop_name: str, key: str, *, retired_default: int | None) -> None:
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(db)
    values = set(config_setting.filter(key=key).values_list("value", flat=True))
    if len(values) > 1:
        msg = (
            f"{key!r} is stored with disagreeing values across scopes ({sorted(values)!r}). "
            f"The {loop_name!r} Loop row is box-global and cannot carry a per-overlay "
            "carve-out: decide the one cadence, set it on the row, then re-run this migration."
        )
        raise DivergentScopedRowsError(msg)
    hours = next(iter(values), retired_default)
    if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0:
        apps.get_model("core", "Loop").objects.using(db).filter(name=loop_name).update(
            delay_seconds=hours * SECONDS_PER_HOUR
        )
    config_setting.filter(key=key).delete()
