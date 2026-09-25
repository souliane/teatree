"""The one fold a retired cadence key gets: its hours onto the Loop row that owned the timer.

A cadence stored in a ``ConfigSetting`` row AND on the ``Loop`` row it gated is two clocks
in series, so retiring the key is right — but the operator's stored hours belong ON the row
rather than in the bin, and the fold has to happen BEFORE the key disappears.

Shared by 0092 and 0096. A migration is a frozen record, so this stays behaviour-identical:
a new fold shape gets a new function here rather than editing this one.
"""

from collections.abc import Callable

from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

SECONDS_PER_HOUR = 3600

type LoopKeyFolds = tuple[tuple[str, str], ...]


class DivergentScopedRowsError(RuntimeError):
    """The stored rows disagree across scopes, so no single box-global row can replace them."""


def fold_cadence(folds: LoopKeyFolds) -> Callable[[StateApps, BaseDatabaseSchemaEditor], None]:
    """A ``RunPython`` forward that moves each key's stored hours onto its loop, then deletes it."""

    def run(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        config_setting = apps.get_model("core", "ConfigSetting")
        loop = apps.get_model("core", "Loop")
        for loop_name, key in folds:
            values = set(config_setting.objects.filter(key=key).values_list("value", flat=True))
            if len(values) > 1:
                msg = (
                    f"{key!r} is stored with disagreeing values across scopes ({sorted(values)!r}). "
                    f"The {loop_name!r} Loop row is box-global and cannot carry a per-overlay "
                    "carve-out: decide the one cadence, set it on the row, then re-run this migration."
                )
                raise DivergentScopedRowsError(msg)
            hours = next(iter(values), None)
            if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0:
                loop.objects.filter(name=loop_name).update(delay_seconds=hours * SECONDS_PER_HOUR)
            config_setting.objects.filter(key=key).delete()

    return run
