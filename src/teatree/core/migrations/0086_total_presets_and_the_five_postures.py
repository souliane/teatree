"""Make every preset total, and land the owner's five postures (B1/B2/B3/B6).

Totality first, from the live box: each preset's missing entries are filled from that
loop's own ``Loop.enabled`` — the exact value the absent entry used to inherit — so the
fill changes no verdict on any box. Keys naming no live loop are dropped; they resolved
to nothing already.

Then the postures, landed from INTENT rather than from what this box happened to be running
(B13). ``present`` runs every loop and each posture below it subtracts: ``present`` >= ``afk``
>= ``maintenance`` >= ``off``, so stepping down a posture can only ever remove work. Filling
``present`` from the box's effective values instead is what made it narrower than ``afk`` —
eighteen loops ran only while the owner was away. ``token-outage`` sits outside the chain: it
is defined by a property (a loop needs no tokens), not by a position in it.

``off`` carried the token-budget table, not "off", so it is RENAMED
``token-outage`` and ``low-token`` MERGES into it — the two shipped byte-identical masks,
and a box where they diverge is refused rather than resolved by picking one. A real
all-false ``off`` is created in its place. ``away`` becomes ``afk`` and gains the owner's
posture: the daily job without a voice. ``maintenance`` becomes self-repair rather than
drain-only. Both declare ``egress = "forbid"`` — the AFK line is per-action, read at the
on-behalf chokepoint, so masking loops could never have expressed it.

``factory-solo`` was an early ``afk`` and goes; anything naming it is re-pointed at
``afk``. To rebuild it: ``t3 loop preset create factory-solo``, then edit it from ``afk``.

Reverse is ``RunPython.noop``: after the merge a ``token-outage`` row that was ``off`` is
indistinguishable from one that was ``low-token``, so a reverse would fork one row into
two. Recovery is forward — the idempotent seed plus ``t3 loop preset use <name>``.
"""

from django.db import migrations, models

_TOKEN_OUTAGE = "token-outage"  # noqa: S105 — a preset name, not a credential
_MERGED_INTO_TOKEN_OUTAGE = ("off", "low-token")
_OLD_AFK, _AFK = "away", "afk"
_OLD_SCHEDULE, _SCHEDULE = "always-away", "always-afk"
_DELETED_PRESETS = {"factory-solo": _AFK}

_SETTING_RENAMES = {
    "low_power_auto_engage": "token_outage_auto_engage",
    "low_power_preset_name": "token_outage_preset_name",
}
_PRESET_VALUED_SETTINGS = ("default_mode", "presence_upgrade_mode", "token_outage_preset_name")
_SCHEDULE_VALUED_SETTING = "active_loop_schedule"

_MAINTENANCE_ON = frozenset(
    {
        "ci_eval_heal",
        "db_backup",
        "dispatch",
        "housekeeping",
        "idle_stack_reaper",
        "inbox",
        "local_stack_queue",
        "resource_pressure",
        "snapshot_warmer",
    }
)
_AFK_OFF = frozenset({"directive_loop"})


_DESCRIPTIONS = {
    "present": "Do everything: every loop runs and the factory acts on the owner's behalf.",
    _AFK: (
        "The daily job with no colleague interaction at all: implement and take intake, but never "
        "review a colleague's MR, never answer an inbound review comment, and never post on the "
        "owner's behalf."
    ),
    "maintenance": (
        "Self-repair only: keep the box healthy, drain the local queue, heal CI and back up — "
        "no delivery, no intake, no outward voice."
    ),
    _TOKEN_OUTAGE: "Token-budget guard: only the loops that never call a model stay up.",
    "off": (
        "Completely off. Nothing reaps stacks, nothing answers Slack — recovery is SSH, docker or "
        "django-admin, and the switch reports what it strands."
    ),
}
_SCHEDULE_DESCRIPTION = "AFK all week: the factory keeps taking work and never posts on the owner's behalf."


def _totalize(mode, loop_states: dict[str, bool]) -> None:
    for preset in mode.objects.all():
        stored = preset.entries if isinstance(preset.entries, dict) else {}
        preset.entries = {
            name: stored[name] if isinstance(stored.get(name), bool) else enabled
            for name, enabled in sorted(loop_states.items())
        }
        preset.save(update_fields=["entries"])


def _repoint(apps, old: str, new: str) -> None:
    apps.get_model("core", "ModeOverride").objects.filter(preset_name=old).update(preset_name=new)
    apps.get_model("core", "ModeScheduleSlot").objects.filter(preset_name=old).update(preset_name=new)
    config_setting = apps.get_model("core", "ConfigSetting")
    for key in _PRESET_VALUED_SETTINGS:
        config_setting.objects.filter(key=key, value=old).update(value=new)


def _merge_into_token_outage(apps, mode) -> None:
    rows = [row for name in _MERGED_INTO_TOKEN_OUTAGE if (row := mode.objects.filter(name=name).first())]
    if len(rows) == len(_MERGED_INTO_TOKEN_OUTAGE) and rows[0].entries != rows[1].entries:
        loops = set(rows[0].entries) | set(rows[1].entries)
        divergent = sorted(loop for loop in loops if rows[0].entries.get(loop) != rows[1].entries.get(loop))
        refusal = (
            f"'{rows[0].name}' and '{rows[1].name}' disagree on {', '.join(divergent)}; they merge into "
            f"'{_TOKEN_OUTAGE}', so reconcile them deliberately and re-run"
        )
        raise RuntimeError(refusal)
    for name in _MERGED_INTO_TOKEN_OUTAGE:
        _repoint(apps, name, _TOKEN_OUTAGE)
    for row in rows[1:]:
        mode.objects.filter(pk=row.pk).delete()
    if rows:
        mode.objects.filter(pk=rows[0].pk).update(name=_TOKEN_OUTAGE, description=_DESCRIPTIONS[_TOKEN_OUTAGE])


def _land_postures(apps, mode, loop_names: set[str]) -> None:
    mode.objects.filter(name="present").update(
        entries=dict.fromkeys(sorted(loop_names), True), description=_DESCRIPTIONS["present"]
    )
    _repoint(apps, _OLD_AFK, _AFK)
    afk = mode.objects.filter(name__in=(_OLD_AFK, _AFK)).first()
    if afk is not None:
        mode.objects.filter(pk=afk.pk).update(
            name=_AFK,
            entries={name: name not in _AFK_OFF for name in sorted(loop_names)},
            egress="forbid",
            description=_DESCRIPTIONS[_AFK],
        )
    mode.objects.filter(name="maintenance").update(
        entries={name: name in _MAINTENANCE_ON for name in sorted(loop_names)},
        egress="forbid",
        description=_DESCRIPTIONS["maintenance"],
    )
    mode.objects.get_or_create(
        name="off",
        defaults={"entries": dict.fromkeys(sorted(loop_names), False), "description": _DESCRIPTIONS["off"]},
    )


def _land(apps, schema_editor) -> None:
    loop = apps.get_model("core", "Loop")
    mode = apps.get_model("core", "Mode")
    loop_states = dict(loop.objects.values_list("name", "enabled"))

    _totalize(mode, loop_states)
    _merge_into_token_outage(apps, mode)
    _land_postures(apps, mode, set(loop_states))

    for name, successor in _DELETED_PRESETS.items():
        _repoint(apps, name, successor)
        mode.objects.filter(name=name).delete()

    apps.get_model("core", "ModeSchedule").objects.filter(name=_OLD_SCHEDULE).update(
        name=_SCHEDULE, description=_SCHEDULE_DESCRIPTION
    )
    config_setting = apps.get_model("core", "ConfigSetting")
    config_setting.objects.filter(key=_SCHEDULE_VALUED_SETTING, value=_OLD_SCHEDULE).update(value=_SCHEDULE)
    for old_key, new_key in _SETTING_RENAMES.items():
        config_setting.objects.filter(key=old_key).update(key=new_key)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0085_scanner_overlay_scope_leaves_the_preset"),
    ]

    operations = [
        migrations.AddField(
            model_name="mode",
            name="egress",
            field=models.CharField(
                choices=[("allow", "Acts on the owner's behalf"), ("forbid", "Never posts on the owner's behalf")],
                default="allow",
                max_length=16,
            ),
        ),
        migrations.RunPython(_land, migrations.RunPython.noop),
    ]
