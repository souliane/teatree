"""Refuse a box carrying an overlay-scoped row for a key its reader cannot honour.

Nine loop-timer keys are one clock per box: the scanner each feeds is built with
``overlay=""``, and the machine-wide tick cadence resolves through whichever overlay the
process happens to be. The write seam now refuses such a row, so this is the one-off check
that a box does not already carry one.

It REFUSES rather than deleting: the row is an operator's deliberate value, and which
value the box should now carry globally is their decision, not a pick this migration can
make.
"""

from django.db import migrations

BOX_GLOBAL_SCALARS = (
    "backlog_sweep_cadence_hours",
    "dogfood_smoke_cadence_hours",
    "eval_local_cadence_hours",
    "idle_stack_reaper_cadence_minutes",
    "loop_cadence_seconds",
    "resource_pressure_cadence_minutes",
    "scanning_news_cadence_hours",
    "self_update_cadence_hours",
    "snapshot_warmer_max_age_days",
)


class UnhonourableOverlayScopeError(RuntimeError):
    """A stored row is scoped to an overlay its key's reader can never consult."""


def _refuse_overlay_scoped_rows(apps, schema_editor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    scoped = config_setting.filter(key__in=BOX_GLOBAL_SCALARS).exclude(scope="")
    rows = sorted((row.key, row.scope, row.value) for row in scoped)
    if rows:
        msg = (
            f"overlay-scoped rows for box-global loop timers: {rows!r}. Their readers resolve no "
            "overlay, so each row is either unreachable or decided by whichever overlay the "
            "process happens to be. Decide the one value, write it at the GLOBAL scope "
            "(`t3 <overlay> config_setting set <key> <value>`), clear the scoped row, then re-run."
        )
        raise UnhonourableOverlayScopeError(msg)


class Migration(migrations.Migration):
    dependencies = [("core", "0090_the_preset_alone_admits_the_nine_global_loops")]

    operations = [migrations.RunPython(_refuse_overlay_scoped_rows, migrations.RunPython.noop)]
