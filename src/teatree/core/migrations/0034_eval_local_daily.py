from django.apps.registry import Apps
from django.db import migrations


def upgrade_eval_local_cadence(apps: Apps, _schema_editor: object) -> None:
    loop_model = apps.get_model("core", "Loop")
    shipped = loop_model.objects.filter(name="eval_local", delay_seconds=604800)
    shipped.filter(description="Runs the local behavioral eval suite weekly; this row IS the cadence.").update(
        description="Runs one shard of behavioral evals daily on the factory subscription account."
    )
    shipped.update(delay_seconds=86400)


class Migration(migrations.Migration):
    dependencies = [("core", "0033_config_setting_change")]

    operations = [migrations.RunPython(upgrade_eval_local_cadence, migrations.RunPython.noop)]
