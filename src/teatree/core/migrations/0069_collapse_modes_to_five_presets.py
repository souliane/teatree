"""Collapse the seven pre-decision modes to the five decided presets (#4202).

``engaged`` → ``present``, ``low-power`` → ``low-token``, ``unattended`` + ``offline``
→ ``away``, ``heads-down`` is cut, and ``maintenance`` is redefined to drain rather
than idle (``ship``/``review`` ON, ``tickets``/``issue_implementer`` OFF). Every name
a row can point at travels with the rename — schedule slots, the manual override, and
the four ``ConfigSetting`` values naming a mode or the renamed holiday schedule — so
no layer is left dangling at a mode that no longer exists.

The three posture booleans go with them: with ``offline`` merged away no preset sets
``pauses_self_pump`` or clears ``presence_sensitive``, and ``defers_questions`` was
retired by #4045. A mode is now a pure per-loop on/off table.
"""

from django.db import migrations

_RENAMES = (("engaged", "present"), ("low-power", "low-token"), ("unattended", "away"))
#: ``offline`` MERGES into ``away`` — its references move, its row goes. Never a rename:
#: its holiday all-off mask now belongs to the redefined ``maintenance``, so carrying it
#: onto ``away`` would give the intake-taking preset a mask that admits no work at all.
#: With no ``unattended`` to rename, ``away`` is simply absent until the idempotent seed
#: creates it from the shipped table — which is the only source of the RIGHT mask.
_MERGED = (("offline", "away"),)
_DROPPED = ("heads-down",)

# Descriptions are refreshed only when the row still carries the SHIPPED pre-rename
# text, so an operator who edited one keeps their wording.
_DESCRIPTIONS = {
    "present": (
        "Full working-hours mode: deliver, interact, keep improvement loops warm.",
        "Full working-hours mode: deliver, interact, keep improvement loops warm.",
    ),
    "away": (
        "The factory keeps producing while the human is unreachable; colleague-facing loops off.",
        "The factory keeps taking new work while the owner is unreachable; the colleague-facing loop is off.",
    ),
    "low-token": (
        "Token-budget guard: only deterministic model-free local loops stay up.",
        "Token-budget guard: only deterministic model-free local loops stay up.",
    ),
    "maintenance": (
        "Nights: self-maintenance + self-improvement only, no ticket/colleague/delivery work.",
        (
            "Drain-only (doubles as the holiday preset): finish and merge what is in flight, "
            "run the consolidation loops, take no new intake."
        ),
    ),
}

# `maintenance` is redefined, not renamed: it drains in-flight work instead of idling.
_MAINTENANCE_ENTRIES = {"ship": True, "review": True, "tickets": False, "issue_implementer": False}

_SCHEDULE_RENAME = ("always-unattended", "always-away")
_SCHEDULE_DESCRIPTIONS = ("The holiday calendar: unattended all week.", "The holiday calendar: away all week.")

# `ConfigSetting` rows whose VALUE is a mode name, and the one whose value is the
# renamed schedule's name. A dangling value here silently falls open to base config.
_MODE_VALUED_SETTINGS = ("default_mode", "presence_upgrade_mode", "low_power_preset_name")
_SCHEDULE_VALUED_SETTING = "active_loop_schedule"

_AUTO_LOW_POWER_REASONS = ("auto:low-power (usage window parked)", "auto:low-token (usage window parked)")


def _rename_modes(apps, schema_editor) -> None:
    mode = apps.get_model("core", "Mode")
    renamed: dict[str, str] = {}
    for old, new in _RENAMES:
        row = mode.objects.filter(name=old).first()
        if row is None:
            continue
        if mode.objects.filter(name=new).exists():
            # The successor name is already taken (an operator's own row): keep theirs
            # rather than colliding on the unique name.
            row.delete()
        else:
            row.name = new
            row.save(update_fields=["name"])
        renamed[old] = new
    for old, new in _MERGED:
        mode.objects.filter(name=old).delete()
        renamed[old] = new
    mode.objects.filter(name__in=_DROPPED).delete()

    for name, (shipped, replacement) in _DESCRIPTIONS.items():
        mode.objects.filter(name=name, description=shipped).update(description=replacement)

    row = mode.objects.filter(name="maintenance").first()
    if row is not None:
        entries = dict(row.entries) if isinstance(row.entries, dict) else {}
        row.entries = {**entries, **_MAINTENANCE_ENTRIES}
        row.save(update_fields=["entries"])

    _repoint_references(apps, renamed)


def _repoint_references(apps, renamed: dict[str, str]) -> None:
    """Move every stored reference to a renamed mode / schedule onto its successor."""
    slot = apps.get_model("core", "ModeScheduleSlot")
    override = apps.get_model("core", "ModeOverride")
    setting = apps.get_model("core", "ConfigSetting")
    for old, new in renamed.items():
        slot.objects.filter(preset_name=old).update(preset_name=new)
        override.objects.filter(preset_name=old).update(preset_name=new)
        setting.objects.filter(key__in=_MODE_VALUED_SETTINGS, value=old).update(value=new)
    slot.objects.filter(preset_name__in=_DROPPED).delete()
    override.objects.filter(preset_name__in=_DROPPED).delete()
    setting.objects.filter(key__in=_MODE_VALUED_SETTINGS, value__in=_DROPPED).delete()

    old_reason, new_reason = _AUTO_LOW_POWER_REASONS
    override.objects.filter(reason=old_reason).update(reason=new_reason)

    schedule = apps.get_model("core", "ModeSchedule")
    old_name, new_name = _SCHEDULE_RENAME
    row = schedule.objects.filter(name=old_name).first()
    if row is not None and not schedule.objects.filter(name=new_name).exists():
        row.name = new_name
        row.save(update_fields=["name"])
        setting.objects.filter(key=_SCHEDULE_VALUED_SETTING, value=old_name).update(value=new_name)
    shipped, replacement = _SCHEDULE_DESCRIPTIONS
    schedule.objects.filter(name=new_name, description=shipped).update(description=replacement)


class Migration(migrations.Migration):
    dependencies = [("core", "0068_rename_headless_max_turns")]

    operations = [
        migrations.RunPython(_rename_modes, migrations.RunPython.noop),
        migrations.RemoveField(model_name="mode", name="defers_questions"),
        migrations.RemoveField(model_name="mode", name="pauses_self_pump"),
        migrations.RemoveField(model_name="mode", name="presence_sensitive"),
    ]
