from django.db import migrations, models


def blank_self_pump_drivers(apps, schema_editor):
    loop_lease = apps.get_model("core", "LoopLease")
    loop_lease.objects.using(schema_editor.connection.alias).filter(driver="self_pump").update(driver="")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_squashed"),
    ]

    operations = [
        migrations.RunPython(blank_self_pump_drivers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="looplease",
            name="driver",
            field=models.CharField(
                blank=True,
                choices=[("loop_runner", "Loop Runner"), ("external", "External")],
                default="",
                max_length=16,
            ),
        ),
    ]
