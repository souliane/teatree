"""``Loop.enabled`` becomes the tri-state manual override, and the forced plane folds into it.

Two planes said the same thing. ``LoopState.forced`` was "a human forced this loop on/off"
and ``Loop.enabled`` was a base tier nothing consulted once presets became total — so the
column becomes the manual layer (A3) and the forced rows move into it value-for-value,
reasons and all. ``forced_until`` becomes ``override_expected_lift_at``: the same instant,
no longer enforced. Nothing auto-clears an override any more (A4/A5), so a TTL that has
already passed is kept as the date the owner MEANT to lift by, and the watcher reminds.

The base tier is nulled first: a ``True`` there was the shipped opt-in posture, not a
human's intervention, and the totalize pass in ``0086`` already copied it into every
preset. Keeping it would turn every shipped default into a permanent override. A ``False``
on a box that has been running loops is different: it is a loop that box keeps off, and a
total preset naming it ``True`` would start it on the next tick. It becomes an ``off``
override carrying a reason, so the loop stays off until someone lifts it on purpose. A box
that has never run a loop has no behaviour to keep, so a fresh install takes the shipped
presets as they are.

``loop_runner_enabled = false`` maps onto the new stop mechanism — an ``off`` override
carrying its own reason — so a box someone stopped stays stopped until it is lifted
deliberately, and any restart elsewhere is the owner's own decision.

``ModeOverride`` loses its TTL in the same pass: ``until`` becomes ``expected_lift_at``,
advisory, and a row that recorded no reason gains one saying so — a reason is required
from here on because without it nothing can judge whether an override still applies (A8).

Reverse restores the forced columns EMPTY: the fold is lossy in one direction only (a
migrated row is indistinguishable from an override set afterwards), so re-deriving it
would invent history. Recovery is forward — set the override again by hand.
"""

from django.db import migrations, models
from django.utils import timezone

_RUNNER_SETTING = "loop_runner_enabled"
_RETIRED_SETTINGS = ("presence_upgrade_mode",)
_STOPPED_REASON = "migrated from loop_runner_enabled=false — lift deliberately"
_FORCED_ON = "on"
_DISABLED_REASON = "migrated from Loop.enabled=false: this loop was off before the upgrade — lift deliberately"


def _fold(apps, schema_editor) -> None:
    db = schema_editor.connection.alias
    loop = apps.get_model("core", "Loop").objects.using(db)
    loop_state = apps.get_model("core", "LoopState").objects.using(db)
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(db)

    mode_override = apps.get_model("core", "ModeOverride").objects.using(db)
    mode_override.filter(reason="").update(reason="migrated from an override that recorded no reason")

    has_run = loop.exclude(last_run_at=None).exists() or loop.exclude(last_attempt_at=None).exists()
    disabled = list(loop.filter(enabled=False).values_list("name", flat=True)) if has_run else []
    loop.update(enabled=None, override_reason="", override_set_at=None, override_expected_lift_at=None)
    loop.filter(name__in=disabled).update(
        enabled=False, override_reason=_DISABLED_REASON, override_set_at=timezone.now()
    )
    for row in loop_state.exclude(forced__in=("", "neutral")):
        loop.filter(name=row.name).update(
            enabled=row.forced == _FORCED_ON,
            override_reason=row.forced_reason or f"migrated from a forced-{row.forced} LoopState override",
            override_set_at=row.updated_at,
            override_expected_lift_at=row.forced_until,
        )

    if _runner_stopped(config_setting):
        mode_override.all().delete()
        mode_override.create(preset_name="off", reason=_STOPPED_REASON)
    config_setting.filter(key__in=(_RUNNER_SETTING, *_RETIRED_SETTINGS)).delete()


def _runner_stopped(config_setting) -> bool:
    """Did this box carry an explicit ``loop_runner_enabled = false``, in any scope?"""
    return any(
        row.value is False or str(row.value).strip().lower() in {"false", "0", "no"}
        for row in config_setting.filter(key=_RUNNER_SETTING)
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0086_total_presets_and_the_five_postures"),
    ]

    operations = [
        migrations.RenameField(model_name="modeoverride", old_name="until", new_name="expected_lift_at"),
        migrations.AlterField(model_name="modeoverride", name="reason", field=models.TextField()),
        migrations.AddField(
            model_name="loop",
            name="override_expected_lift_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="loop",
            name="override_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="loop",
            name="override_set_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="loop",
            name="enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(_fold, migrations.RunPython.noop),
        migrations.RemoveField(model_name="loopstate", name="forced"),
        migrations.RemoveField(model_name="loopstate", name="forced_reason"),
        migrations.RemoveField(model_name="loopstate", name="forced_until"),
    ]
