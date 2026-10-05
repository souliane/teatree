from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0125_convert_stored_data_to_current_formats")]

    operations = [
        migrations.AlterModelTable(name="workergeneration", table="teatree_worker_generation"),
        migrations.AlterModelTable(name="intakescancursor", table="teatree_intake_scan_cursor"),
    ]
