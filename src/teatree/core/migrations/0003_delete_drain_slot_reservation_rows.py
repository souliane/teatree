from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def delete_drain_slot_reservation_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    config_setting.objects.using(schema_editor.connection.alias).filter(key="drain_slot_reservation").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_drop_self_pump_driver"),
    ]

    operations = [
        migrations.RunPython(delete_drain_slot_reservation_rows, migrations.RunPython.noop),
    ]
