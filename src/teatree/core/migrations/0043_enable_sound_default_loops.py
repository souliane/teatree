"""Enable the sound operational default loops (reversing the #2513 "seeded paused" cutover for the core set).

Policy change from the #2513 cutover: the local/read-only operational core now
ships ENABLED so a fresh or restored install works out of the box. Colleague-facing,
externally-visible, destructive-capable, and token-costly loops stay opt-in. Only
rows still carrying the untouched seed default (``enabled=False``, no
``paused``/``disabled`` ``LoopState`` hold) are flipped — an operator's explicit
disable/pause is never clobbered. Reverse flips the ON-set back off except loops
the operator explicitly enabled (a ``LoopState(status="enabled")`` row).

A migration is frozen history and must not import the evolving ``teatree.loops.seed``
module, so the ON-set is INLINED; ``teatree.loops.seed.DEFAULT_LOOPS`` carries the
same ``default_enabled=True`` marks (pinned by the parity test), so the migrate-path
and ``t3 setup`` enable the same 8 loops.
"""

from django.db import migrations

_SOUND_DEFAULT_ON = (
    "inbox",
    "dispatch",
    "tickets",
    "housekeeping",
    "idle_stack_reaper",
    "local_stack_queue",
    "resource_pressure",
    "pane_reaper",
)
_HELD = ("paused", "disabled")


def _enable_sound_default_loops(apps, schema_editor):
    Loop = apps.get_model("core", "Loop")
    LoopState = apps.get_model("core", "LoopState")
    held = LoopState.objects.filter(status__in=_HELD).values_list("name", flat=True)
    Loop.objects.filter(name__in=_SOUND_DEFAULT_ON, enabled=False).exclude(name__in=held).update(enabled=True)


def _disable_sound_default_loops(apps, schema_editor):
    Loop = apps.get_model("core", "Loop")
    LoopState = apps.get_model("core", "LoopState")
    explicitly_enabled = LoopState.objects.filter(status="enabled").values_list("name", flat=True)
    Loop.objects.filter(name__in=_SOUND_DEFAULT_ON, enabled=True).exclude(name__in=explicitly_enabled).update(
        enabled=False
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0042_alter_deferredquestion_resolved_via")]
    operations = [migrations.RunPython(_enable_sound_default_loops, _disable_sound_default_loops)]
