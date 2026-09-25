"""Clear the stored rows under the retired ``dream_promotion_cap``.

A dream pass now batches every promotion into ONE ticket, so the cap has no reader. A
surviving row makes ``retired_settings.warn_removed_setting`` print a loud stderr line on
every resolution, on the statusline/hook/gate hot path. The key is a literal, as in 0109.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

CLEARED_KEY = "dream_promotion_cap"


def clear_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    config_setting.filter(key=CLEARED_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0114_alter_task_failure_kind_and_more")]

    operations = [migrations.RunPython(clear_rows, migrations.RunPython.noop)]
