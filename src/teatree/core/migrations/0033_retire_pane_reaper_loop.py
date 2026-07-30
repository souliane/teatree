"""Drop the ``pane_reaper`` loop from ALREADY-migrated databases (#3734).

The agent-teams pane layer is retired, so its mini-loop no longer exists on disk.
The inlined ``0001`` seeds stop creating the row, but that only reaches a fresh
install — a deployed box keeps a ``Loop`` row whose ``script`` resolves to a
deleted module, which ``build_loop_table_jobs`` raises on rather than silently
skipping. The seeded ``Mode.entries`` carry the same name, which
``preset_findings`` reports as "entries name unknown loops".

Both operations are idempotent and re-runnable. ``backward`` is deliberately a
no-op: the loop module is gone, so recreating the row would resurrect the exact
unresolvable-script state this migration exists to clear.
"""

from django.db import migrations

_PANE_REAPER = "pane_reaper"


def forward(apps, schema_editor) -> None:
    loop = apps.get_model("core", "Loop")
    loop_state = apps.get_model("core", "LoopState")
    mode = apps.get_model("core", "Mode")
    db_alias = schema_editor.connection.alias

    loop.objects.using(db_alias).filter(name=_PANE_REAPER).delete()
    loop_state.objects.using(db_alias).filter(name=_PANE_REAPER).delete()
    # Iterated in Python rather than a JSON lookup: the preset table is a handful
    # of rows and this stays identical across every DB backend teatree runs on.
    for row in mode.objects.using(db_alias).all():
        if _PANE_REAPER in row.entries:
            del row.entries[_PANE_REAPER]
            row.save(update_fields=["entries"])


def backward(apps, schema_editor) -> None:
    """No-op — the pane-reaper loop module no longer exists to point a row at."""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_close_orphaned_sessions"),
    ]

    operations = [migrations.RunPython(forward, backward)]
