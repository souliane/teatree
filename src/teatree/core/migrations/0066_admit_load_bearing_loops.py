"""Every mode admits the load-bearing tier — healing rows written before the guard (#4188).

``off`` masked ``resource_pressure`` and ``idle_stack_reaper`` off while ``db_backup`` kept
writing: the one mask that can only ever consume disk, reached exactly when an operator
grabs a "stop everything" mode mid-incident. The seed is ``get_or_create``, so correcting
the shipped table cannot reach a row that already exists — this does.

The names are inlined rather than imported because a migration is the frozen record of one
historical edit: editing the tier must never retroactively change what this ran.
"""

from django.db import migrations

_LOAD_BEARING = ("housekeeping", "idle_stack_reaper", "inbox", "local_stack_queue", "resource_pressure")
_LOW_POWER_SETTING = "low_power_preset_name"
_DEFAULT_LOW_POWER = "low-power"

#: A halt mode's description read "every loop off", which the heal makes untrue. Rewritten
#: only where it still matches the text that shipped it, so an operator's own wording stands.
_RESTATED_DESCRIPTIONS = {
    "off": (
        "Every Loop-table loop off (the reversible 'calendar says nothing runs' mode).",
        (
            "Every WORK loop off (the reversible 'calendar says nothing runs' mode); the load-bearing tier "
            "stays up so the box can still relieve itself."
        ),
    ),
    "offline": (
        "Holiday: every loop off, questions defer AND the self-pump pauses (was 'off' preset + 'away').",
        "Holiday: every WORK loop off, questions defer AND the self-pump pauses; the load-bearing tier stays up.",
    ),
}


def _admit_load_bearing_loops(apps, schema_editor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    mode = apps.get_model("core", "Mode")
    pinned = config_setting.objects.filter(scope="", key=_LOW_POWER_SETTING).first()
    raw = pinned.value if pinned is not None else None
    escape = raw.strip() if isinstance(raw, str) and raw.strip() else _DEFAULT_LOW_POWER
    for preset in mode.objects.exclude(name=escape):
        entries = preset.entries if isinstance(preset.entries, dict) else {}
        quieted = [loop for loop in _LOAD_BEARING if entries.get(loop) is False]
        shipped, restated = _RESTATED_DESCRIPTIONS.get(preset.name, ("", ""))
        stale_description = bool(shipped) and preset.description == shipped
        if not quieted and not stale_description:
            continue
        preset.entries = {**entries, **dict.fromkeys(quieted, True)}
        if stale_description:
            preset.description = restated
        preset.save(update_fields=["entries", "description", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("core", "0065_interactivedispatch")]

    operations = [migrations.RunPython(_admit_load_bearing_loops, migrations.RunPython.noop)]
