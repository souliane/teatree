"""Land the #3658 sweep loop and the #3649 directive cadence on ALREADY-migrated databases.

``0001_initial`` seeds the canonical loop set, but it has already been applied
everywhere that matters — editing its inlined copy reaches a fresh install and nothing
else. Without this migration the deployed box would simply have no ``dm_sweep`` row: the
fan-out would find nothing to tick and the feature would be dark with no exception, no
red test, and no signal of any kind. That silent shape is the whole reason this file
exists.

Both operations are idempotent and re-runnable. The cadence retune is
provenance-aware: it moves ``directive_loop`` off the old 86400s default ONLY while the
row still carries that exact value, so an operator who chose their own cadence is never
overridden — the same "never clobber an edited row" discipline the install-time seed
keeps.
"""

from django.db import migrations

_DM_SWEEP = "dm_sweep"
_DM_SWEEP_DESCRIPTION = (
    "Sweeps the owner's DM threads hourly and resolves the ones that no longer need "
    "them (owner already replied, subject merged/closed, duplicate of an open thread); "
    "leaves anything older than a day for the resurfacing side, and says nothing when "
    "it resolved nothing."
)
_DIRECTIVE_LOOP = "directive_loop"
_OLD_DIRECTIVE_CADENCE = 86400
_NEW_DIRECTIVE_CADENCE = 3600


def forward(apps, schema_editor) -> None:
    loop = apps.get_model("core", "Loop")
    db_alias = schema_editor.connection.alias
    loop.objects.using(db_alias).get_or_create(
        name=_DM_SWEEP,
        defaults={
            "delay_seconds": 3600,
            "script": f"src/teatree/loops/{_DM_SWEEP}/loop.py",
            "description": _DM_SWEEP_DESCRIPTION,
            "colleague_facing": False,
            "enabled": False,
        },
    )
    loop.objects.using(db_alias).filter(name=_DIRECTIVE_LOOP, delay_seconds=_OLD_DIRECTIVE_CADENCE).update(
        delay_seconds=_NEW_DIRECTIVE_CADENCE
    )


def backward(apps, schema_editor) -> None:
    loop = apps.get_model("core", "Loop")
    db_alias = schema_editor.connection.alias
    loop.objects.using(db_alias).filter(name=_DM_SWEEP).delete()
    loop.objects.using(db_alias).filter(name=_DIRECTIVE_LOOP, delay_seconds=_NEW_DIRECTIVE_CADENCE).update(
        delay_seconds=_OLD_DIRECTIVE_CADENCE
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_session_todo"),
    ]

    operations = [migrations.RunPython(forward, backward)]
