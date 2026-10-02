from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0097_a_loss_free_artifact_sweep_has_its_own_stamp"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourcepressuremarker",
            name="last_artifact_plan",
            field=models.TextField(blank=True, default=""),
        ),
    ]
