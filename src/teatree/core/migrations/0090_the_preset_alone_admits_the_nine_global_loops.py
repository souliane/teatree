"""Drop the nine per-loop existence scalars a total preset made a second answer (D6).

Each gated the whole of ONE loop's only scanner, so the preset entry for that loop
already carries the same opinion. The stored value is NOT folded onto the preset: these
were read through ``load_config().user``, which is the dataclass defaults rather than the
store, so a stored ``true`` was never in effect — folding it would STOP a loop that has
been running all along. The rows go, and ``RETIRED_SETTINGS`` answers for any that a
downgraded box writes back.

Refuses rather than picks when a box's rows DISAGREE across scopes, mirroring ``0088``:
that shape is an operator's deliberate carve-out and is worth a human look before it is
discarded, even though neither value was reaching its reader.
"""

from django.db import migrations

RETIRED_SCALARS = (
    "backlog_sweep_disabled",
    "db_backup_disabled",
    "dogfood_smoke_disabled",
    "eval_local_disabled",
    "idle_stack_reaper_disabled",
    "local_stack_queue_disabled",
    "resource_pressure_disabled",
    "scanning_news_disabled",
    "snapshot_warmer_disabled",
)


class DivergentScopedRowsError(RuntimeError):
    """The stored rows disagree across scopes, so no single preset entry can replace them."""


def _drop_the_existence_scalars(apps, schema_editor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    for key in RETIRED_SCALARS:
        values = set(config_setting.filter(key=key).values_list("value", flat=True))
        if len(values) > 1:
            msg = (
                f"{key!r} is stored with disagreeing values across scopes ({sorted(values)!r}). "
                "A preset entry is box-global and cannot carry a per-overlay carve-out: decide the "
                "one answer, write it into the active preset, then re-run this migration."
            )
            raise DivergentScopedRowsError(msg)
        config_setting.filter(key=key).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0089_the_backup_row_is_the_cadence")]

    operations = [migrations.RunPython(_drop_the_existence_scalars, migrations.RunPython.noop)]
