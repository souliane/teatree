"""Consolidate the critic/directive feature flags (#104) and delete ambient detection (#105).

Three stale-``ConfigSetting`` clean-ups, one migration:

* ``design_critic_live`` — the field folded into ``directive_loop_enabled`` (the
    advisory-only design critic is armed by the directive loop), so its stored rows
    are dead and deleted.
* ``ambient_directive_detection_enabled`` — ambient directive detection is deleted
    wholesale (#105); the only ``Directive`` producer is the explicit
    ``Directive.objects.capture``. Its stored rows are dead and deleted.
* ``critic_gate_live`` (bool) -> ``critic_gate_mode`` (``off|advisory|blocking``): a
    truthy row carried today's ENFORCING posture, so it becomes ``blocking``; a falsy
    row was the dark default, so it simply falls through to the ``off`` default (no
    ``critic_gate_mode`` row written). The old ``critic_gate_live`` row is removed
    either way. Migrated per-scope (global + each overlay) independently.

Idempotent — re-running finds no legacy rows and is a no-op.
"""

from django.db import migrations

_DESIGN_CRITIC_KEY = "design_critic_live"
_AMBIENT_KEY = "ambient_directive_detection_enabled"
_CRITIC_LIVE_KEY = "critic_gate_live"
_CRITIC_MODE_KEY = "critic_gate_mode"
_CRITIC_MODE_BLOCKING = "blocking"


def consolidate_critic_and_ambient_flags(apps, schema_editor):
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    ConfigSetting.objects.filter(key__in=[_DESIGN_CRITIC_KEY, _AMBIENT_KEY]).delete()
    for row in ConfigSetting.objects.filter(key=_CRITIC_LIVE_KEY):
        if row.value:
            ConfigSetting.objects.update_or_create(
                scope=row.scope,
                key=_CRITIC_MODE_KEY,
                defaults={"value": _CRITIC_MODE_BLOCKING},
            )
        row.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_usagewindowstate_task_not_before"),
    ]

    operations = [
        migrations.RunPython(consolidate_critic_and_ambient_flags, migrations.RunPython.noop),
    ]
