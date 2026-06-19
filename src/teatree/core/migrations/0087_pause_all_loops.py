"""Pause every loop for the #2513 cutover — the cutover is plumbing only.

Migration 0078 seeded the default loops ``enabled=True``. The #2513 cutover
re-points the live tick, the statusline, and ``t3 loop list`` at the ``Loop``
table, but ships with every loop PAUSED: no loop ticks until an operator
deliberately enables it. This migration disables every currently-enabled row so
the cutover lands on an existing DB (the seeded rows from 0078) with nothing
running. The install-time seed (``teatree.loops.seed``) lands fresh rows paused
too, so both the migration path and the squashed-install path agree.

Reverse re-enables the default seeded loop set (the 0078 intent), so the
migration is honestly reversible without re-enabling operator-created rows.
"""

from django.db import migrations

_DEFAULT_LOOP_NAMES = frozenset(
    {
        "inbox",
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
        "dispatch",
        "tickets",
        "review",
        "ship",
        "pane_reaper",
        "issue_implementer",
        "housekeeping",
        "audit",
        "followup",
        "arch_review",
        "dogfood",
        "eval_local",
        "news",
        "dream",
        "slack_answer",
    },
)


def _pause_all(apps, schema_editor) -> None:
    Loop = apps.get_model("core", "Loop")
    Loop.objects.filter(enabled=True).update(enabled=False)


def _restore_defaults(apps, schema_editor) -> None:
    Loop = apps.get_model("core", "Loop")
    Loop.objects.filter(name__in=_DEFAULT_LOOP_NAMES).update(enabled=True)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0086_prompt_params_and_version"),
    ]

    operations = [
        migrations.RunPython(_pause_all, _restore_defaults),
    ]
