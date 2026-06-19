"""Backfill ``Worktree.overlay`` from the ticket where it was left empty (#1397).

The cwd auto-detect path (``resolve_worktree`` → ``get_or_create``) materialised
worktree rows with ``overlay=''`` because its ``defaults`` never set ``overlay``,
even though the row's ticket carried the real overlay. A blank ``Worktree.overlay``
made the per-overlay ``max_concurrent_local_stacks`` gate miss those rows when it
scoped its blocker query by ``Worktree.overlay`` — letting a second stack breach
the cap. The source fix sets ``overlay`` on new rows; this backfills the existing
rows so the data is correct everywhere.

Forward: copy ``ticket.overlay`` onto every worktree whose ``overlay`` is empty
and whose ticket has a non-empty overlay. Irreversible (a no-op reverse) — the
pre-fix blank state carried no information to restore to.
"""

from django.db import migrations


def _backfill_overlay(apps, schema_editor) -> None:
    worktree_model = apps.get_model("core", "Worktree")
    to_fix = list(
        worktree_model.objects.filter(overlay="").exclude(ticket__overlay="").select_related("ticket"),
    )
    for worktree in to_fix:
        worktree.overlay = worktree.ticket.overlay
    if to_fix:
        worktree_model.objects.bulk_update(to_fix, ["overlay"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0087_pause_all_loops"),
    ]

    operations = [
        migrations.RunPython(_backfill_overlay, migrations.RunPython.noop),
    ]
