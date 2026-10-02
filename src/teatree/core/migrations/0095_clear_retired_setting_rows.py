"""Clear the stored rows under two keys the code no longer has a field for.

A row under a REMOVED key resolves to nothing and emits a loud stderr line naming the
key on every resolution (``retired_settings.warn_removed_setting``) — never-lockout by
design, but noise once the removal is settled. These two are the only retired keys the
three-box sweep measured stored rows for, and every one of them holds what was the
shipped default, so deleting them changes no effective value.

The list is a literal rather than a read of ``REMOVED_SETTING_KEYS``: a migration states
what it did to the rows that existed when it ran, and a later retirement must not
retroactively widen an applied cleanup (0027/0086 precedent).
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

CLEARED_KEYS = ("limit_autorecovery_enabled", "loop_runner_enabled")


def clear_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    config_setting.filter(key__in=CLEARED_KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0094_name_the_harness_control_timeout")]

    operations = [migrations.RunPython(clear_rows, migrations.RunPython.noop)]
