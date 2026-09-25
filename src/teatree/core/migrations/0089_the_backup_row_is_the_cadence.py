"""Drop the inner backup cadence a wall-clock row anchor replaced (Shape B).

An operator's stored hours cannot migrate onto the row: the row's ``daily_at`` names a TIME,
not an interval, so there is nothing to carry. The row is reseeded from the shipped table.
"""

from django.db import migrations

RETIRED_KEY = "db_backup_cadence_hours"


def _drop_the_inner_cadence(apps, schema_editor) -> None:
    apps.get_model("core", "ConfigSetting").objects.filter(key=RETIRED_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0088_the_preset_alone_admits_a_loop")]

    operations = [migrations.RunPython(_drop_the_inner_cadence, migrations.RunPython.noop)]
