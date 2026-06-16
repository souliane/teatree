"""Seed the autonomous loops as :class:`Loop` rows (#1796).

A one-time data migration that seeds one row per autonomous loop — each with its
own cadence (``delay_seconds``, or ``daily_at`` for once-per-day loops). The
loops' logic stays in their existing Python code; the row only carries config +
cadence. ``get_or_create`` leaves an existing operator-edited row intact;
reversible.
"""

import datetime as dt

from django.db import migrations

# (name, delay_seconds, daily_at, enabled) — one autonomous loop per row.
_SEED_LOOPS = (
    ("inbox", 60, None, True),
    ("idle_stack_reaper", 60, None, True),
    ("local_stack_queue", 60, None, True),
    ("resource_pressure", 60, None, True),
    ("dispatch", 300, None, True),
    ("tickets", 300, None, True),
    ("review", 300, None, True),
    ("ship", 300, None, True),
    ("pane_reaper", 300, None, True),
    ("issue_implementer", 3600, None, True),
    ("housekeeping", 3600, None, True),
    ("audit", 1800, None, True),
    ("followup", 1800, None, True),
    ("arch_review", 10800, None, True),
    ("dogfood", 86400, None, True),
    ("eval_local", 86400, None, True),
    ("news", 86400, dt.time(8, 0), True),
    ("dream", 86400, dt.time(3, 0), True),
    ("slack_answer", 20, None, True),
)


def _seed(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    for name, delay, daily_at, enabled in _SEED_LOOPS:
        loop_model.objects.get_or_create(
            name=name,
            defaults={
                "delay_seconds": delay,
                "daily_at": daily_at,
                "enabled": enabled,
                "prompt": f"Run a sub-agent to run the {name} loop.",
            },
        )


def _unseed(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    loop_model.objects.filter(name__in=[name for name, *_ in _SEED_LOOPS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0077_loop"),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
