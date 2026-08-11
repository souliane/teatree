"""Collapse the seven pre-decision modes to the five decided presets (#4202).

``engaged`` → ``present``, ``low-power`` → ``low-token``, ``unattended`` → ``away``,
``offline`` → ``off``, ``heads-down`` is cut, and ``maintenance`` is redefined to drain
rather than idle (``ship``/``review`` ON, ``tickets``/``issue_implementer`` OFF). Every
name a row can point at travels with the rename — schedule slots, the manual override,
and the four ``ConfigSetting`` values naming a mode or the renamed holiday schedule — so
no layer is left dangling at a mode that no longer exists.

The three posture booleans go with them: with ``offline`` merged away no preset sets
``pauses_self_pump`` or clears ``presence_sensitive``, and ``defers_questions`` was
retired by #4045. A mode is now a pure per-loop on/off table.

Reverse is ``RunPython.noop`` because an honest reverse is impossible, not merely
unwritten: post-merge an ``off`` row that WAS ``offline`` is indistinguishable from one
that always was, so a reverse would fork one row into two, and ``heads-down`` row data
is reconstructible from no shipped table. Migrating back to ``0070`` restores the three
columns at their field defaults (``False``/``False``/``True``) — the correct posture for
``off`` — so the pre-collapse resolver reads a coherent table. Recovery is forward:
``t3 loop preset use <name>`` plus the idempotent seed.

One behaviour change the mask does not capture: old ``offline`` set
``presence_sensitive=False``, so a SCHEDULE- or DEFAULT-sourced holiday was never
presence-upgraded by a keystroke. With the column deleted a schedule/default ``off`` IS
upgradable (:func:`teatree.core.mode_resolution._apply_presence_upgrade`). A manual
``--hold`` override is source ``override`` and is still never upgraded, so an operator's
explicit hold is unaffected.
"""

from django.db import migrations

_RENAMES = (("engaged", "present"), ("low-power", "low-token"), ("unattended", "away"))
#: ``offline`` MERGES into ``off`` — its references move, its row goes. The two shipped a
#: byte-identical ``entries`` mask and differed ONLY in the three posture booleans this
#: migration drops, so once those columns are gone they are the same mode and the merge is
#: a true dedupe. A successor that re-admits ``tickets``/``ship``/``dispatch`` would take
#: new intake under a hold the operator set precisely to stop it.
_MERGED = (("offline", "off"),)
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
        "Drain-only: finish and merge what is in flight, run the consolidation loops, take no new intake.",
    ),
    "off": (
        (
            "Every WORK loop off (the reversible 'calendar says nothing runs' mode); the "
            "load-bearing tier stays up so the box can still relieve itself."
        ),
        (
            "Every WORK loop off — the hard hold (a holiday, or a calendar that says nothing "
            "runs); the load-bearing tier stays up so the box can still relieve itself."
        ),
    ),
}

# `maintenance` is redefined, not renamed: it drains in-flight work instead of idling.
_MAINTENANCE_ENTRIES = {"ship": True, "review": True, "tickets": False, "issue_implementer": False}

_SCHEDULE_RENAME = ("always-unattended", "always-away")
_SCHEDULE_DESCRIPTIONS = (
    "The holiday calendar: unattended all week.",
    "Away all week: the owner is unreachable, the factory keeps taking work.",
)

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
    dependencies = [("core", "0070_task_owner_driving_since")]

    operations = [
        migrations.RunPython(_rename_modes, migrations.RunPython.noop),
        migrations.RemoveField(model_name="mode", name="defers_questions"),
        migrations.RemoveField(model_name="mode", name="pauses_self_pump"),
        migrations.RemoveField(model_name="mode", name="presence_sensitive"),
    ]
