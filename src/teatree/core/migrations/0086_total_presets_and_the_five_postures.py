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
``token-outage`` and ``low-token`` MERGES into it — the two shipped byte-identical masks.
Where a box let them diverge, each disputed loop takes the shipped ``token-outage`` value
and the choice is logged: a migration that refuses leaves the whole upgrade half-applied,
which is worse than the one loop it would have protected. A real all-false ``off`` is
created in its place. ``away`` becomes ``afk`` and gains the owner's posture: the daily
job without a voice. ``maintenance`` becomes self-repair rather than drain-only. Both
declare ``egress = "forbid"`` — the AFK line is per-action, read at the on-behalf
chokepoint, so masking loops could never have expressed it.

A posture is only rewritten while it still holds what teatree last shipped. A row the
operator edited keeps its entries and description: the new posture is a default, and
landing it over a deliberate edit would change what the box runs overnight with nobody
having decided that. The kept row is logged so the difference is visible. On a box that
has been running loops, even a rewritten posture only ever REMOVES: a loop it names on is
kept on only where that preset already ran it, so the upgrade starts nothing new — the
default-off loops ``Loop.enabled`` held back stay off. A fresh install has no behaviour to
keep and takes the new postures as they are.

``factory-solo`` was an early ``afk`` and goes; anything naming it is re-pointed at
``afk``. To rebuild it: ``t3 loop preset create factory-solo``, then edit it from ``afk``.

Reverse is ``RunPython.noop``: after the merge a ``token-outage`` row that was ``off`` is
indistinguishable from one that was ``low-token``, so a reverse would fork one row into
two. Recovery is forward — the idempotent seed plus ``t3 loop preset use <name>``.
"""

import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)

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

# What the last release shipped for each rewritten posture. A row still equal to it was
# never edited, so landing the new posture over it overrides no one.
_PREVIOUSLY_SHIPPED = {
    "present": dict.fromkeys(
        [
            "inbox",
            "dispatch",
            "tickets",
            "ship",
            "review",
            "followup",
            "audit",
            "news",
            "arch_review",
            "dream",
            "eval_local",
            "dogfood",
            "snapshot_warmer",
            "housekeeping",
            "idle_stack_reaper",
            "local_stack_queue",
            "resource_pressure",
        ],
        True,
    ),
    _OLD_AFK: {
        **dict.fromkeys(
            [
                "inbox",
                "dispatch",
                "tickets",
                "ship",
                "review",
                "audit",
                "news",
                "arch_review",
                "dream",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            ],
            True,
        ),
        "followup": False,
    },
    "maintenance": {
        **dict.fromkeys(
            [
                "inbox",
                "dispatch",
                "ship",
                "review",
                "dream",
                "eval_local",
                "dogfood",
                "arch_review",
                "news",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            ],
            True,
        ),
        **dict.fromkeys(["tickets", "issue_implementer", "followup", "audit"], False),
    },
}

_PREVIOUSLY_SHIPPED_DESCRIPTIONS = {
    "present": {"Full working-hours mode: deliver, interact, keep improvement loops warm."},
    _OLD_AFK: {
        "The factory keeps producing while the human is unreachable; colleague-facing loops off.",
        "The factory keeps taking new work while the owner is unreachable; the colleague-facing loop is off.",
    },
    "maintenance": {
        "Nights: self-maintenance + self-improvement only, no ticket/colleague/delivery work.",
        "Drain-only: finish and merge what is in flight, run the consolidation loops, take no new intake.",
    },
}

# The shipped ``token-outage`` opinion, the tie-break for a loop ``off`` and ``low-token``
# disagree on. A loop it does not name stays off: the preset is a token guard.
_SHIPPED_TOKEN_OUTAGE_ON = frozenset(
    [
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
        "snapshot_warmer",
        "issue_disposition",
        "followup",
        "dm_sweep",
        "housekeeping",
        "db_backup",
        "ratchet_repair",
        "memory_skim",
    ]
)


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
    for preset in mode.all():
        stored = preset.entries if isinstance(preset.entries, dict) else {}
        preset.entries = {
            name: stored[name] if isinstance(stored.get(name), bool) else enabled
            for name, enabled in sorted(loop_states.items())
        }
        preset.save(update_fields=["entries"])


def _repoint(apps, db: str, old: str, new: str) -> None:
    apps.get_model("core", "ModeOverride").objects.using(db).filter(preset_name=old).update(preset_name=new)
    apps.get_model("core", "ModeScheduleSlot").objects.using(db).filter(preset_name=old).update(preset_name=new)
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(db)
    for key in _PRESET_VALUED_SETTINGS:
        config_setting.filter(key=key, value=old).update(value=new)


def _merge_into_token_outage(apps, db: str, mode) -> None:
    rows = [row for name in _MERGED_INTO_TOKEN_OUTAGE if (row := mode.filter(name=name).first())]
    merged = dict(rows[0].entries) if rows else {}
    if len(rows) == len(_MERGED_INTO_TOKEN_OUTAGE) and rows[0].entries != rows[1].entries:
        loops = set(rows[0].entries) | set(rows[1].entries)
        divergent = sorted(loop for loop in loops if rows[0].entries.get(loop) != rows[1].entries.get(loop))
        for loop in divergent:
            merged[loop] = loop in _SHIPPED_TOKEN_OUTAGE_ON
        logger.warning(
            "'%s' and '%s' disagree on %s; '%s' takes the shipped value for each (%s)",
            rows[0].name,
            rows[1].name,
            ", ".join(divergent),
            _TOKEN_OUTAGE,
            ", ".join(f"{loop}={merged[loop]}" for loop in divergent),
        )
    for name in _MERGED_INTO_TOKEN_OUTAGE:
        _repoint(apps, db, name, _TOKEN_OUTAGE)
    for row in rows[1:]:
        mode.filter(pk=row.pk).delete()
    if rows:
        mode.filter(pk=rows[0].pk).update(name=_TOKEN_OUTAGE, entries=merged, description=_DESCRIPTIONS[_TOKEN_OUTAGE])


def _edited_by_the_operator(name: str, stored: dict[str, tuple[dict, str]]) -> bool:
    if name not in stored:
        return False
    entries, description = stored[name]
    edited = entries != _PREVIOUSLY_SHIPPED[name] or description not in _PREVIOUSLY_SHIPPED_DESCRIPTIONS[name]
    if edited:
        logger.warning(
            "preset '%s' differs from what teatree shipped; keeping the operator's entries and description", name
        )
    return edited


def _within_what_ran(mode, name: str, landed: dict[str, bool], *, has_run: bool) -> dict[str, bool]:
    if not has_run:
        return landed
    row = mode.filter(name=name).first()
    ran = row.entries if row is not None and isinstance(row.entries, dict) else {}
    return {loop: on and ran.get(loop) is True for loop, on in landed.items()}


def _land_postures(apps, db: str, mode, loop_names: set[str], stored: dict[str, tuple[dict, str]]) -> None:
    loop = apps.get_model("core", "Loop").objects.using(db)
    has_run = loop.exclude(last_run_at=None).exists() or loop.exclude(last_attempt_at=None).exists()
    if not _edited_by_the_operator("present", stored):
        mode.filter(name="present").update(
            entries=_within_what_ran(mode, "present", dict.fromkeys(sorted(loop_names), True), has_run=has_run),
            description=_DESCRIPTIONS["present"],
        )
    _repoint(apps, db, _OLD_AFK, _AFK)
    afk = mode.filter(name__in=(_OLD_AFK, _AFK)).first()
    if afk is not None:
        mode.filter(pk=afk.pk).update(name=_AFK, egress="forbid")
        if not _edited_by_the_operator(_OLD_AFK, stored):
            landed = {name: name not in _AFK_OFF for name in sorted(loop_names)}
            mode.filter(pk=afk.pk).update(
                entries=_within_what_ran(mode, _AFK, landed, has_run=has_run),
                description=_DESCRIPTIONS[_AFK],
            )
    mode.filter(name="maintenance").update(egress="forbid")
    if not _edited_by_the_operator("maintenance", stored):
        landed = {name: name in _MAINTENANCE_ON for name in sorted(loop_names)}
        mode.filter(name="maintenance").update(
            entries=_within_what_ran(mode, "maintenance", landed, has_run=has_run),
            description=_DESCRIPTIONS["maintenance"],
        )
    mode.get_or_create(
        name="off",
        defaults={"entries": dict.fromkeys(sorted(loop_names), False), "description": _DESCRIPTIONS["off"]},
    )


def _land(apps, schema_editor) -> None:
    db = schema_editor.connection.alias
    loop = apps.get_model("core", "Loop").objects.using(db)
    mode = apps.get_model("core", "Mode").objects.using(db)
    loop_states = dict(loop.values_list("name", "enabled"))
    stored = {row.name: (row.entries, row.description) for row in mode.filter(name__in=_PREVIOUSLY_SHIPPED)}

    _totalize(mode, loop_states)
    _merge_into_token_outage(apps, db, mode)
    _land_postures(apps, db, mode, set(loop_states), stored)

    for name, successor in _DELETED_PRESETS.items():
        _repoint(apps, db, name, successor)
        mode.filter(name=name).delete()

    apps.get_model("core", "ModeSchedule").objects.using(db).filter(name=_OLD_SCHEDULE).update(
        name=_SCHEDULE, description=_SCHEDULE_DESCRIPTION
    )
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(db)
    config_setting.filter(key=_SCHEDULE_VALUED_SETTING, value=_OLD_SCHEDULE).update(value=_SCHEDULE)
    for old_key, new_key in _SETTING_RENAMES.items():
        config_setting.filter(key=old_key).update(key=new_key)


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
